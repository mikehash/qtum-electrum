# Electrum - lightweight Bitcoin client
# Copyright (C) 2018 The Electrum Developers
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import time
import threading
from collections import defaultdict
from functools import reduce
import itertools
from operator import itemgetter

from . import qtum
from .qtum import COINBASE_MATURITY, TYPE_ADDRESS, TYPE_PUBKEY, b58_address_to_hash160, TOKEN_TRANSFER_TOPIC, TYPE_STAKE
from .util import PrintError, profiler, bfh, bh2u, VerifiedTxInfo, TxMinedInfo
from .transaction import Transaction, TxOutput
from .synchronizer import Synchronizer
from .verifier import SPV
from .blockchain import hash_header
from .i18n import _
from .tokens import Tokens

TX_HEIGHT_LOCAL = -2
TX_HEIGHT_UNCONF_PARENT = -1
TX_HEIGHT_UNCONFIRMED = 0


class AddTransactionException(Exception):
    pass


class UnrelatedTransactionException(AddTransactionException):
    def __str__(self):
        return _("Transaction is unrelated to this wallet.")


class AddressSynchronizer(PrintError):
    """
    inherited by wallet
    """

    def __init__(self, storage):
        self.storage = storage
        self.network = None
        # verifier (SPV) and synchronizer are started in start_threads
        self.synchronizer = None
        self.verifier = None
        # locks: if you need to take multiple ones, acquire them in the order they are defined here!
        self.lock = threading.RLock()
        self.transaction_lock = threading.RLock()
        self.token_lock = threading.RLock()
        # address -> list(txid, height)
        self.history = storage.get('addr_history',{})

        # Token
        self.tokens = Tokens(self.storage)
        # contract_addr + '_' + b58addr -> list(txid, height, log_index)
        self.token_history = storage.get('addr_token_history', {})
        # txid -> tx receipt
        self.tx_receipt = storage.get('tx_receipt', {})

        # Verified transactions.  txid -> VerifiedTxInfo.  Access with self.lock.
        verified_tx = storage.get('verified_tx3', {})
        self.verified_tx = {}
        for txid, (height, timestamp, txpos, header_hash) in verified_tx.items():
            self.verified_tx[txid] = VerifiedTxInfo(height, timestamp, txpos, header_hash)
        # Transactions pending verification.  txid -> tx_height. Access with self.lock.
        self.unverified_tx = defaultdict(int)
        # true when synchronized
        self.up_to_date = False
        # thread local storage for caching stuff
        self.threadlocal_cache = threading.local()
        self._get_addr_balance_cache = {}
        self.load_and_cleanup()

    def with_transaction_lock(func):
        def func_wrapper(self, *args, **kwargs):
            with self.transaction_lock:
                return func(self, *args, **kwargs)
        return func_wrapper

    def load_and_cleanup(self):
        self.load_transactions()
        self.load_local_history()
        self.load_token_txs()
        self.check_history()
        self.check_token_history()
        self.load_unverified_transactions()
        self.remove_local_transactions_we_dont_have()

    def is_mine(self, address):
        return address in self.history

    def get_addresses(self):
        return sorted(self.history.keys())

    def get_address_history(self, addr):
        h = []
        # we need self.transaction_lock but get_tx_height will take self.lock
        # so we need to take that too here, to enforce order of locks
        with self.lock, self.transaction_lock:
            related_txns = self._history_local.get(addr, set())
            for tx_hash in related_txns:
                tx_height = self.get_tx_height(tx_hash).height
                h.append((tx_hash, tx_height))
        return h

    def get_address_history_len(self, addr: str) -> int:
        return len(self._history_local.get(addr, ()))

    def get_txin_address(self, txi):
        addr = txi.get('address')
        if addr and addr != "(pubkey)":
            return addr
        prevout_hash = txi.get('prevout_hash')
        prevout_n = txi.get('prevout_n')
        dd = self.txo.get(prevout_hash, {})
        for addr, l in dd.items():
            for n, v, is_cb in l:
                if n == prevout_n:
                    return addr
        return None

    def get_txout_address(self, txo: TxOutput):
        if txo.type == TYPE_ADDRESS:
            addr = txo.address
        elif txo.type == TYPE_PUBKEY:
            addr = qtum.public_key_to_p2pkh(bfh(txo.address))
        else:
            addr = None
        return addr

    def load_unverified_transactions(self):
        # review transactions that are in the history
        for addr, hist in self.history.items():
            for tx_hash, tx_height in hist:
                # add it in case it was previously unconfirmed
                self.add_unverified_tx(tx_hash, tx_height)

        # qtum
        # review transactions that are in the token history
        for key, token_hist in self.token_history.items():
            for txid, height, log_index in token_hist:
                self.add_unverified_tx(txid, height)

    def start_network(self, network):
        self.network = network
        if self.network is not None:
            self.verifier = SPV(self.network, self)
            self.synchronizer = Synchronizer(self, network)
            network.add_jobs([self.verifier, self.synchronizer])
            self.network.register_callback(self.on_blockchain_updated, ['blockchain_updated'])
        else:
            self.verifier = None
            self.synchronizer = None

    def on_blockchain_updated(self, event, *args):
        self._get_addr_balance_cache = {}  # invalidate cache

    def stop_threads(self):
        if self.network:
            self.network.remove_jobs([self.synchronizer, self.verifier])
            self.synchronizer.release()
            self.synchronizer = None
            self.verifier = None
            self.network.unregister_callback(self.on_blockchain_updated)
            # Now no references to the synchronizer or verifier
            # remain so they will be GC-ed
            self.storage.put('stored_height', self.get_local_height())
        self.save_transactions()
        self.save_verified_tx()
        self.storage.write()

    def add_address(self, address):
        if address not in self.history:
            self.history[address] = []
            self.set_up_to_date(False)
        if self.synchronizer:
            self.synchronizer.add(address)

    def get_conflicting_transactions(self, tx):
        """Returns a set of transaction hashes from the wallet history that are
        directly conflicting with tx, i.e. they have common outpoints being
        spent with tx. If the tx is already in wallet history, that will not be
        reported as a conflict.
        """
        conflicting_txns = set()
        with self.transaction_lock:
            for txin in tx.inputs():
                if txin['type'] == 'coinbase':
                    continue
                prevout_hash = txin['prevout_hash']
                prevout_n = txin['prevout_n']
                spending_tx_hash = self.spent_outpoints[prevout_hash].get(prevout_n)
                if spending_tx_hash is None:
                    continue
                # this outpoint has already been spent, by spending_tx
                assert spending_tx_hash in self.transactions
                conflicting_txns |= {spending_tx_hash}
            txid = tx.txid()
            if txid in conflicting_txns:
                # this tx is already in history, so it conflicts with itself
                if len(conflicting_txns) > 1:
                    raise Exception('Found conflicting transactions already in wallet history.')
                conflicting_txns -= {txid}
            return conflicting_txns

    def add_transaction(self, tx_hash, tx, allow_unrelated=False):
        assert tx_hash, tx_hash
        assert tx, tx
        assert tx.is_complete()
        # we need self.transaction_lock but get_tx_height will take self.lock
        # so we need to take that too here, to enforce order of locks
        with self.lock, self.transaction_lock:
            # NOTE: returning if tx in self.transactions might seem like a good idea
            # BUT we track is_mine inputs in a txn, and during subsequent calls
            # of add_transaction tx, we might learn of more-and-more inputs of
            # being is_mine, as we roll the gap_limit forward

            # qtum
            is_coinbase = tx.inputs()[0]['type'] == 'coinbase' or tx.outputs()[0].type == TYPE_STAKE
            tx_height = self.get_tx_height(tx_hash).height
            if not allow_unrelated:
                # note that during sync, if the transactions are not properly sorted,
                # it could happen that we think tx is unrelated but actually one of the inputs is is_mine.
                # this is the main motivation for allow_unrelated
                is_mine = any([self.is_mine(self.get_txin_address(txin)) for txin in tx.inputs()])
                is_for_me = any([self.is_mine(self.get_txout_address(txo)) for txo in tx.outputs()])
                if not is_mine and not is_for_me:
                    raise UnrelatedTransactionException()
            # Find all conflicting transactions.
            # In case of a conflict,
            #     1. confirmed > mempool > local
            #     2. this new txn has priority over existing ones
            # When this method exits, there must NOT be any conflict, so
            # either keep this txn and remove all conflicting (along with dependencies)
            #     or drop this txn
            conflicting_txns = self.get_conflicting_transactions(tx)
            if conflicting_txns:
                existing_mempool_txn = any(
                    self.get_tx_height(tx_hash2).height in (TX_HEIGHT_UNCONFIRMED, TX_HEIGHT_UNCONF_PARENT)
                    for tx_hash2 in conflicting_txns)
                existing_confirmed_txn = any(
                    self.get_tx_height(tx_hash2).height > 0
                    for tx_hash2 in conflicting_txns)
                if existing_confirmed_txn and tx_height <= 0:
                    # this is a non-confirmed tx that conflicts with confirmed txns; drop.
                    return False
                if existing_mempool_txn and tx_height == TX_HEIGHT_LOCAL:
                    # this is a local tx that conflicts with non-local txns; drop.
                    return False
                # keep this txn and remove all conflicting
                to_remove = set()
                to_remove |= conflicting_txns
                for conflicting_tx_hash in conflicting_txns:
                    to_remove |= self.get_depending_transactions(conflicting_tx_hash)
                for tx_hash2 in to_remove:
                    self.remove_transaction(tx_hash2)
            # add inputs
            def add_value_from_prev_output():
                dd = self.txo.get(prevout_hash, {})
                # note: this nested loop takes linear time in num is_mine outputs of prev_tx
                for addr, outputs in dd.items():
                    # note: instead of [(n, v, is_cb), ...]; we could store: {n -> (v, is_cb)}
                    for n, v, is_cb in outputs:
                        if n == prevout_n:
                            if addr and self.is_mine(addr):
                                if d.get(addr) is None:
                                    d[addr] = set()
                                d[addr].add((ser, v))
                                self._get_addr_balance_cache.pop(addr, None)  # invalidate cache
                            return
            self.txi[tx_hash] = d = {}
            for txi in tx.inputs():
                if txi['type'] == 'coinbase':
                    continue
                prevout_hash = txi['prevout_hash']
                prevout_n = txi['prevout_n']
                ser = prevout_hash + ':%d' % prevout_n
                self.spent_outpoints[prevout_hash][prevout_n] = tx_hash
                add_value_from_prev_output()
            # add outputs
            self.txo[tx_hash] = d = {}
            for n, txo in enumerate(tx.outputs()):
                v = txo[2]
                ser = tx_hash + ':%d'%n
                addr = self.get_txout_address(txo)
                if addr and self.is_mine(addr):
                    if d.get(addr) is None:
                        d[addr] = []
                    d[addr].append((n, v, is_coinbase))
                    self._get_addr_balance_cache.pop(addr, None)  # invalidate cache
                    # give v to txi that spends me
                    next_tx = self.spent_outpoints[tx_hash].get(n)
                    if next_tx is not None:
                        dd = self.txi.get(next_tx, {})
                        if dd.get(addr) is None:
                            dd[addr] = set()
                        if (ser, v) not in dd[addr]:
                            dd[addr].add((ser, v))
                        self._add_tx_to_local_history(next_tx)
            # add to local history
            self._add_tx_to_local_history(tx_hash)
            # save
            self.transactions[tx_hash] = tx
            return True

    def remove_transaction(self, tx_hash):
        def remove_from_spent_outpoints():
            # undo spends in spent_outpoints
            if tx is not None:  # if we have the tx, this branch is faster
                for txin in tx.inputs():
                    if txin['type'] == 'coinbase':
                        continue
                    prevout_hash = txin['prevout_hash']
                    prevout_n = txin['prevout_n']
                    self.spent_outpoints[prevout_hash].pop(prevout_n, None)
                    if not self.spent_outpoints[prevout_hash]:
                        self.spent_outpoints.pop(prevout_hash)
            else:  # expensive but always works
                for prevout_hash, d in list(self.spent_outpoints.items()):
                    for prevout_n, spending_txid in d.items():
                        if spending_txid == tx_hash:
                            self.spent_outpoints[prevout_hash].pop(prevout_n, None)
                            if not self.spent_outpoints[prevout_hash]:
                                self.spent_outpoints.pop(prevout_hash)
            # Remove this tx itself; if nothing spends from it.
            # It is not so clear what to do if other txns spend from it, but it will be
            # removed when those other txns are removed.
            if not self.spent_outpoints[tx_hash]:
                self.spent_outpoints.pop(tx_hash)

        with self.transaction_lock:
            self.print_error("removing tx from history", tx_hash)
            tx = self.transactions.pop(tx_hash, None)
            remove_from_spent_outpoints()
            self._remove_tx_from_local_history(tx_hash)
            for addr in itertools.chain(self.txi.get(tx_hash, {}).keys(), self.txo.get(tx_hash, {}).keys()):
                self._get_addr_balance_cache.pop(addr, None)  # invalidate cache
            self.txi.pop(tx_hash, None)
            self.txo.pop(tx_hash, None)

    def receive_tx_callback(self, tx_hash, tx, tx_height):
        self.add_unverified_tx(tx_hash, tx_height)
        self.add_transaction(tx_hash, tx, allow_unrelated=True)

    def receive_history_callback(self, addr, hist, tx_fees):
        with self.lock:
            old_hist = self.get_address_history(addr)
            for tx_hash, height in old_hist:
                if (tx_hash, height) not in hist:
                    # make tx local
                    self.unverified_tx.pop(tx_hash, None)
                    self.verified_tx.pop(tx_hash, None)
                    if self.verifier:
                        self.verifier.remove_spv_proof_for_tx(tx_hash)
            self.history[addr] = hist

        for tx_hash, tx_height in hist:
            # add it in case it was previously unconfirmed
            self.add_unverified_tx(tx_hash, tx_height)
            # if addr is new, we have to recompute txi and txo
            tx = self.transactions.get(tx_hash)
            if tx is None:
                continue
            self.add_transaction(tx_hash, tx, allow_unrelated=True)

        # Store fees
        self.tx_fees.update(tx_fees)

    @profiler
    def load_transactions(self):
        # load txi, txo, tx_fees
        self.txi = self.storage.get('txi', {})
        for txid, d in list(self.txi.items()):
            for addr, lst in d.items():
                self.txi[txid][addr] = set([tuple(x) for x in lst])
        self.txo = self.storage.get('txo', {})
        self.tx_fees = self.storage.get('tx_fees', {})
        tx_list = self.storage.get('transactions', {})
        # load transactions
        self.transactions = {}
        for tx_hash, raw in tx_list.items():
            tx = Transaction(raw)
            self.transactions[tx_hash] = tx
            if self.txi.get(tx_hash) is None and self.txo.get(tx_hash) is None:
                self.print_error("removing unreferenced tx", tx_hash)
                self.transactions.pop(tx_hash)
        # load spent_outpoints
        _spent_outpoints = self.storage.get('spent_outpoints', {})
        self.spent_outpoints = defaultdict(dict)
        for prevout_hash, d in _spent_outpoints.items():
            for prevout_n_str, spending_txid in d.items():
                prevout_n = int(prevout_n_str)
                if spending_txid not in self.transactions:
                    continue
                self.spent_outpoints[prevout_hash][prevout_n] = spending_txid

    def get_depending_transactions(self, tx_hash):
        """Returns all (grand-)children of tx_hash in this wallet."""
        children = set()
        for other_hash in self.spent_outpoints[tx_hash].values():
            children.add(other_hash)
            children |= self.get_depending_transactions(other_hash)
        return children
    @profiler
    def load_local_history(self):
        self._history_local = {}  # address -> set(txid)
        for txid in itertools.chain(self.txi, self.txo):
            self._add_tx_to_local_history(txid)

    @profiler
    def check_history(self):
        save = False
        hist_addrs_mine = list(filter(lambda k: self.is_mine(k), self.history.keys()))
        hist_addrs_not_mine = list(filter(lambda k: not self.is_mine(k), self.history.keys()))
        for addr in hist_addrs_not_mine:
            self.history.pop(addr)
            save = True
        for addr in hist_addrs_mine:
            hist = self.history[addr]
            for tx_hash, tx_height in hist:
                if self.txi.get(tx_hash) or self.txo.get(tx_hash):
                    continue
                tx = self.transactions.get(tx_hash)
                if tx is not None:
                    self.add_transaction(tx_hash, tx, allow_unrelated=True)
                    save = True
        if save:
            self.save_transactions()

    def remove_local_transactions_we_dont_have(self):
        txid_set = set(self.txi) | set(self.txo)
        for txid in txid_set:
            tx_height = self.get_tx_height(txid).height
            if tx_height == TX_HEIGHT_LOCAL and txid not in self.transactions:
                self.remove_transaction(txid)

    @profiler
    def save_transactions(self, write=False):
        with self.transaction_lock:
            tx = {}
            for k,v in self.transactions.items():
                tx[k] = str(v)
            self.storage.put('transactions', tx)
            self.storage.put('txi', self.txi)
            self.storage.put('txo', self.txo)
            self.storage.put('tx_fees', self.tx_fees)
            self.storage.put('addr_history', self.history)
            self.storage.put('addr_token_history', self.token_history)
            self.storage.put('tx_receipt', self.tx_receipt)
            self.storage.put('spent_outpoints', self.spent_outpoints)

            token_txs = {}
            for txid, tx in self.token_txs.items():
                token_txs[txid] = str(tx)
            self.storage.put('token_txs', token_txs)
            if write:
                self.storage.write()

    def save_verified_tx(self, write=False):
        with self.lock:
            self.storage.put('verified_tx3', self.verified_tx)
            if write:
                self.storage.write()

    def clear_history(self):
        with self.lock:
            with self.transaction_lock:
                self.txi = {}
                self.txo = {}
                self.tx_fees = {}
                self.spent_outpoints = defaultdict(dict)
                self.history = {}
                self.verified_tx = {}
                self.token_history = {}
                self.tx_receipt = {}
                self.token_txs = {}
                self.transactions = {}
                self.save_transactions()

    def get_txpos(self, tx_hash):
        """Returns (height, txpos) tuple, even if the tx is unverified."""
        with self.lock:
            if tx_hash in self.verified_tx:
                info = self.verified_tx[tx_hash]
                return info.height, info.txpos
            elif tx_hash in self.unverified_tx:
                height = self.unverified_tx[tx_hash]
                return (height, 0) if height > 0 else ((1e9 - height), 0)
            else:
                return (1e9+1, 0)

    def with_local_height_cached(func):
        # get local height only once, as it's relatively expensive.
        # take care that nested calls work as expected
        def f(self, *args, **kwargs):
            orig_val = getattr(self.threadlocal_cache, 'local_height', None)
            self.threadlocal_cache.local_height = orig_val or self.get_local_height()
            try:
                return func(self, *args, **kwargs)
            finally:
                self.threadlocal_cache.local_height = orig_val
        return f

    @with_local_height_cached
    def get_history(self, domain=None, from_timestamp=None, to_timestamp=None):

        # get domain
        if domain is None:
            domain = self.get_addresses()
        domain = set(domain)
        # 1. Get the history of each address in the domain, maintain the
        #    delta of a tx as the sum of its deltas on domain addresses
        tx_deltas = defaultdict(int)
        for addr in domain:
            h = self.get_address_history(addr)
            for tx_hash, height in h:
                delta = self.get_tx_delta(tx_hash, addr)
                if delta is None or tx_deltas[tx_hash] is None:
                    tx_deltas[tx_hash] = None
                else:
                    tx_deltas[tx_hash] += delta

        # 2. create sorted history
        history = []
        for tx_hash in tx_deltas:
            delta = tx_deltas[tx_hash]
            tx_mined_status = self.get_tx_height(tx_hash)
            history.append((tx_hash, tx_mined_status, delta))

        history.sort(key=lambda x: self.get_txpos(x[0]))
        history.reverse()

        # 3. add balance
        c, u, x = self.get_balance(domain)
        balance = c + u + x
        h2 = []
        now = time.time()
        for tx_hash, tx_mined_status, delta in history:
            if from_timestamp and (tx_mined_status.timestamp or now) < from_timestamp:
                continue
            if to_timestamp and (tx_mined_status.timestamp or now) >= to_timestamp:
                continue
            h2.append((tx_hash, tx_mined_status, delta, balance))
            if balance is None or delta is None:
                balance = None
            else:
                balance -= delta
        h2.reverse()

        if not from_timestamp and not to_timestamp:
            # fixme: this may happen if history is incomplete
            if balance not in [None, 0]:
                self.print_error("Error: history not synchronized")
                return []
        return h2

    def _add_tx_to_local_history(self, txid):
        with self.transaction_lock:
            for addr in itertools.chain(self.txi.get(txid, []), self.txo.get(txid, [])):
                cur_hist = self._history_local.get(addr, set())
                cur_hist.add(txid)
                self._history_local[addr] = cur_hist

    def _remove_tx_from_local_history(self, txid):
        with self.transaction_lock:
            for addr in itertools.chain(self.txi.get(txid, []), self.txo.get(txid, [])):
                cur_hist = self._history_local.get(addr, set())
                try:
                    cur_hist.remove(txid)
                except KeyError:
                    pass
                else:
                    self._history_local[addr] = cur_hist

    def add_unverified_tx(self, tx_hash, tx_height):
        if tx_hash in self.verified_tx:
            if tx_height in (TX_HEIGHT_UNCONFIRMED, TX_HEIGHT_UNCONF_PARENT):
                with self.lock:
                    self.verified_tx.pop(tx_hash)
                if self.verifier:
                    self.verifier.remove_spv_proof_for_tx(tx_hash)
        else:
            with self.lock:
                # tx will be verified only if height > 0
                self.unverified_tx[tx_hash] = tx_height
            # to remove pending proof requests:
            if self.verifier:
                self.verifier.remove_spv_proof_for_tx(tx_hash)

    def add_verified_tx(self, tx_hash: str, info: VerifiedTxInfo):
        # Remove from the unverified map and add to the verified map
        with self.lock:
            self.unverified_tx.pop(tx_hash, None)
            self.verified_tx[tx_hash] = info
        tx_mined_status = self.get_tx_height(tx_hash)
        self.network.trigger_callback('verified', self, tx_hash, tx_mined_status)

    def get_unverified_txs(self):
        '''Returns a map from tx hash to transaction height'''
        with self.lock:
            return dict(self.unverified_tx)  # copy

    def undo_verifications(self, blockchain, height):
        '''Used by the verifier when a reorg has happened'''
        txs = set()
        with self.lock:
            for tx_hash, info in list(self.verified_tx.items()):
                tx_height = info.height
                if tx_height >= height:
                    header = blockchain.read_header(tx_height)
                    if not header or hash_header(header) != info.header_hash:
                        self.verified_tx.pop(tx_hash, None)
                        # NOTE: we should add these txns to self.unverified_tx,
                        # but with what height?
                        # If on the new fork after the reorg, the txn is at the
                        # same height, we will not get a status update for the
                        # address. If the txn is not mined or at a diff height,
                        # we should get a status update. Unless we put tx into
                        # unverified_tx, it will turn into local. So we put it
                        # into unverified_tx with the old height, and if we get
                        # a status update, that will overwrite it.
                        self.unverified_tx[tx_hash] = tx_height
                        txs.add(tx_hash)
        return txs

    def get_local_height(self):
        """ return last known height if we are offline """
        cached_local_height = getattr(self.threadlocal_cache, 'local_height', None)
        if cached_local_height is not None:
            return cached_local_height
        return self.network.get_local_height() if self.network else self.storage.get('stored_height', 0)

    def get_tx_height(self, tx_hash: str) -> TxMinedInfo:
        with self.lock:
            verified_tx_info = self.verified_tx.get(tx_hash, None)
            if verified_tx_info:
                conf = max(self.get_local_height() - verified_tx_info.height + 1, 0)
                return TxMinedInfo(
                    height=verified_tx_info.height,
                    conf=conf,
                    header_hash=verified_tx_info.header_hash,
                    timestamp=verified_tx_info.timestamp,
                )
            elif tx_hash in self.unverified_tx:
                height = self.unverified_tx[tx_hash]
                return TxMinedInfo(height=height, conf=0)
            else:
                # local transaction
                return TxMinedInfo(height=TX_HEIGHT_LOCAL, conf=0)

    def set_up_to_date(self, up_to_date):
        with self.lock:
            self.up_to_date = up_to_date
        if self.network:
            self.network.notify('status')
        if up_to_date:
            self.save_transactions(write=True)
            # if the verifier is also up to date, persist that too;
            # otherwise it will persist its results when it finishes
            if self.verifier and self.verifier.is_up_to_date():
                self.save_verified_tx(write=True)

    def is_up_to_date(self):
        with self.lock: return self.up_to_date

    @with_transaction_lock
    def get_tx_delta(self, tx_hash, address):
        """"effect of tx on address"""
        delta = 0
        # substract the value of coins sent from address
        d = self.txi.get(tx_hash, {}).get(address, [])
        for n, v in d:
            delta -= v
        # add the value of the coins received at address
        d = self.txo.get(tx_hash, {}).get(address, [])
        for n, v, cb in d:
            delta += v
        return delta

    @with_transaction_lock
    def get_tx_value(self, txid):
        """effect of tx on the entire domain"""
        delta = 0
        for addr, d in self.txi.get(txid, {}).items():
            for n, v in d:
                delta -= v
        for addr, d in self.txo.get(txid, {}).items():
            for n, v, cb in d:
                delta += v
        return delta

    def get_wallet_delta(self, tx):
        """ effect of tx on wallet """
        is_relevant = False  # "related to wallet?"
        is_mine = False
        is_pruned = False
        is_partial = False
        v_in = v_out = v_out_mine = 0
        for txin in tx.inputs():
            addr = self.get_txin_address(txin)
            if self.is_mine(addr):
                is_mine = True
                is_relevant = True
                d = self.txo.get(txin['prevout_hash'], {}).get(addr, [])
                for n, v, cb in d:
                    if n == txin['prevout_n']:
                        value = v
                        break
                else:
                    value = None
                if value is None:
                    is_pruned = True
                else:
                    v_in += value
            else:
                is_partial = True
        if not is_mine:
            is_partial = False
        for o in tx.outputs():
            v_out += o.value
            if self.is_mine(o.address):
                v_out_mine += o.value
                is_relevant = True
        if is_pruned:
            # some inputs are mine:
            fee = None
            if is_mine:
                v = v_out_mine - v_out
            else:
                # no input is mine
                v = v_out_mine
        else:
            v = v_out_mine - v_in
            if is_partial:
                # some inputs are mine, but not all
                fee = None
            else:
                # all inputs are mine
                fee = v_in - v_out
        if not is_mine:
            fee = None
        return is_relevant, is_mine, v, fee

    def get_addr_io(self, address):
        with self.lock, self.transaction_lock:
            h = self.get_address_history(address)
            received = {}
            sent = {}
            for tx_hash, height in h:
                l = self.txo.get(tx_hash, {}).get(address, [])
                for n, v, is_cb in l:
                    received[tx_hash + ':%d'%n] = (height, v, is_cb)
            for tx_hash, height in h:
                l = self.txi.get(tx_hash, {}).get(address, [])
                for txi, v in l:
                    sent[txi] = height
        return received, sent

    def get_addr_utxo(self, address):
        coins, spent = self.get_addr_io(address)
        for txi in spent:
            coins.pop(txi)
        out = {}
        for txo, v in coins.items():
            tx_height, value, is_cb = v
            prevout_hash, prevout_n = txo.split(':')
            x = {
                'address':address,
                'value':value,
                'prevout_n':int(prevout_n),
                'prevout_hash':prevout_hash,
                'height':tx_height,
                'coinbase':is_cb
            }
            out[txo] = x
        return out

    # return the total amount ever received by an address
    def get_addr_received(self, address):
        received, sent = self.get_addr_io(address)
        return sum([v for height, v, is_cb in received.values()])

    @with_local_height_cached
    def get_addr_balance(self, address):
        """return the balance of a bitcoin address:
        confirmed and matured, unconfirmed, unmatured
        """
        cached_value = self._get_addr_balance_cache.get(address)
        if cached_value:
            return cached_value
        received, sent = self.get_addr_io(address)
        c = u = x = 0
        local_height = self.get_local_height()
        for txo, (tx_height, v, is_cb) in received.items():
            if is_cb and tx_height + COINBASE_MATURITY > local_height:
                x += v
            elif tx_height > 0:
                c += v
            else:
                u += v
            if txo in sent:
                if sent[txo] > 0:
                    c -= v
                else:
                    u -= v
        result = c, u, x
        # cache result.
        # Cache needs to be invalidated if a transaction is added to/
        # removed from history; or on new blocks (maturity...)
        self._get_addr_balance_cache[address] = result
        return result

    @with_local_height_cached
    def get_utxos(self, domain=None, excluded=None, mature=False, confirmed_only=False):
        coins = []
        if domain is None:
            domain = self.get_addresses()
        domain = set(domain)
        if excluded:
            domain = set(domain) - excluded
        for addr in domain:
            utxos = self.get_addr_utxo(addr)
            for x in utxos.values():
                if confirmed_only and x['height'] <= 0:
                    continue
                if mature and x['coinbase'] and x['height'] + COINBASE_MATURITY > self.get_local_height():
                    continue
                coins.append(x)
                continue
        return coins

    def get_balance(self, domain=None):
        if domain is None:
            domain = self.get_addresses()
        domain = set(domain)
        cc = uu = xx = 0
        for addr in domain:
            c, u, x = self.get_addr_balance(addr)
            cc += c
            uu += u
            xx += x
        return cc, uu, xx

    def is_used(self, address):
        h = self.history.get(address,[])
        return len(h) != 0

    def is_empty(self, address):
        c, u, x = self.get_addr_balance(address)
        return c+u+x == 0

    @profiler
    def load_token_txs(self):
        token_tx_list = self.storage.get('token_txs', {})
        # token_hist_txids = reduce(lambda x, y: x+y, list([[y[0] for y in x] for x in self.token_history.values()]))
        if self.token_history:
            token_hist_txids = [x2[0] for x2 in reduce(lambda x1, y1: x1+y1, self.token_history.values())]
        else:
            token_hist_txids = []
        self.token_txs = {}
        for tx_hash, raw in token_tx_list.items():
            if tx_hash in token_hist_txids:
                tx = Transaction(raw)
                self.token_txs[tx_hash] = tx

    @profiler
    def check_token_history(self):
        # remove not mine and not subscribe token history
        save = False
        hist_keys_not_mine = list(filter(lambda k: not self.is_mine(k.split('_')[1]), self.token_history.keys()))
        hist_keys_not_subscribe = list(filter(lambda k: k not in self.tokens, self.token_history.keys()))
        for key in set(hist_keys_not_mine).union(hist_keys_not_subscribe):
            hist = self.token_history.pop(key)
            for txid, height, log_index in hist:
                self.token_txs.pop(txid)
            save = True
        if save:
            self.save_transactions()

    def receive_token_history_callback(self, key, hist):
        with self.token_lock:
            self.token_history[key] = hist

    def receive_tx_receipt_callback(self, tx_hash, tx_receipt):
        self.add_tx_receipt(tx_hash, tx_receipt)

    def receive_token_tx_callback(self, tx_hash, tx, tx_height):
        self.add_unverified_tx(tx_hash, tx_height)
        self.add_token_transaction(tx_hash, tx)

    def add_tx_receipt(self, tx_hash, tx_receipt):
        assert tx_hash, 'none tx_hash'
        assert tx_receipt, 'empty tx_receipt'
        for contract_call in tx_receipt:
            if not contract_call.get('transactionHash') == tx_hash:
                return
            if not contract_call.get('log'):
                return
        with self.token_lock:
            self.tx_receipt[tx_hash] = tx_receipt

    def add_token_transaction(self, tx_hash, tx):
        with self.token_lock:
            assert tx.is_complete(), 'incomplete tx'
            self.token_txs[tx_hash] = tx
            return True

    def add_token(self, token):
        key = '{}_{}'.format(token.contract_addr, token.bind_addr)
        self.tokens[key] = token
        if self.synchronizer:
            self.synchronizer.add_token(token)

    def delete_token(self, key):
        with self.token_lock:
            if key in self.tokens:
                self.tokens.pop(key)
            if key in self.token_history:
                self.token_history.pop(key)

    def get_token_history(self, contract_addr=None, bind_addr=None, from_timestamp=None, to_timestamp=None):
        with self.lock, self.token_lock:
            h = []  # from, to, amount, token, txid, height, conf, timestamp, call_index, log_index
            keys = []
            for token_key in self.tokens.keys():
                if contract_addr and contract_addr in token_key \
                        or bind_addr and bind_addr in token_key \
                        or not bind_addr and not contract_addr:
                    keys.append(token_key)
            for key in keys:
                contract_addr, bind_addr = key.split('_')
                for txid, height, log_index in self.token_history.get(key, []):
                    status = self.get_tx_height(txid)
                    height, conf, timestamp = status.height, status.conf, status.timestamp
                    for call_index, contract_call in enumerate(self.tx_receipt.get(txid, [])):
                        logs = contract_call.get('log', [])
                        if len(logs) > log_index:
                            log = logs[log_index]

                            # check contarct address
                            if contract_addr != log.get('address', ''):
                                print('contract address mismatch')
                                continue

                            # check topic name
                            topics = log.get('topics', [])
                            if len(topics) < 3:
                                print('not enough topics')
                                continue
                            if topics[0] != TOKEN_TRANSFER_TOPIC:
                                print('topic mismatch')
                                continue

                            # check user bind address
                            _, hash160b = b58_address_to_hash160(bind_addr)
                            hash160 = bh2u(hash160b).zfill(64)
                            if hash160 not in topics:
                                print('address mismatch')
                                continue
                            amount = int(log.get('data'), 16)
                            from_addr = topics[1][-40:]
                            to_addr = topics[2][-40:]
                            h.append(
                                (from_addr, to_addr, amount, self.tokens[key], txid,
                                 height, conf, timestamp, call_index, log_index))
                        else:
                            continue
            return sorted(h, key=itemgetter(5, 8, 9), reverse=True)