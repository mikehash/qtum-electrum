#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 kyuupichan@gmail
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
from collections import defaultdict
from math import floor, log10
from typing import NamedTuple, List

from .bitcoin import sha256, COIN, TYPE_ADDRESS, is_address
from .transaction import Transaction, TxOutput
from .util import NotEnoughFunds, PrintError


# A simple deterministic PRNG.  Used to deterministically shuffle a
# set of coins - the same set of coins should produce the same output.
# Although choosing UTXOs "randomly" we want it to be deterministic,
# so if sending twice from the same UTXO set we choose the same UTXOs
# to spend.  This prevents attacks on users by malicious or stale
# servers.
class PRNG:
    def __init__(self, seed):
        self.sha = sha256(seed)
        self.pool = bytearray()

    def get_bytes(self, n):
        while len(self.pool) < n:
            self.pool.extend(self.sha)
            self.sha = sha256(self.sha)
        result, self.pool = self.pool[:n], self.pool[n:]
        return result

    def randint(self, start, end):
        # Returns random integer in [start, end)
        n = end - start
        r = 0
        p = 1
        while p < n:
            r = self.get_bytes(1)[0] + (r << 8)
            p = p << 8
        return start + (r % n)

    def choice(self, seq):
        return seq[self.randint(0, len(seq))]

    def shuffle(self, x):
        for i in reversed(range(1, len(x))):
            # pick an element in x[:i+1] with which to exchange x[i]
            j = self.randint(0, i+1)
            x[i], x[j] = x[j], x[i]


class Bucket(NamedTuple):
    desc: str
    weight: int
    value: int
    coins: List[dict]
    min_height: int
    witness: bool


def strip_unneeded(bkts, sufficient_funds, exception_addr=None) -> list:
    '''
    Remove buckets that are unnecessary in achieving the spend amount
    exception_addr is the address that cannot be removed
    '''
    if sufficient_funds([], bucket_value_sum=0):
        # none of the buckets are needed
        return []
    bkts = sorted(bkts, key=lambda bkt: bkt.value, reverse=True)
    bucket_value_sum = 0

    # move the exception bucket to the top
    if exception_addr:
        for i in range(len(bkts)):
            if bkts[i].desc == exception_addr:
                exception_bucket = bkts[i]
                del bkts[i]
                bkts.insert(0, exception_bucket)
                break

    for i in range(len(bkts)):
        bucket_value_sum += (bkts[i]).value
        if sufficient_funds(bkts[:i+1], bucket_value_sum=bucket_value_sum):
            return bkts[:i+1]
    raise Exception("keeping all buckets is still not enough")


def strip_unneeded_utxo(bkts, sufficient_funds):
    '''Remove utxos that are unnecessary in achieving the spend amount'''
    assert len(bkts) == 1
    bucket = bkts[0]
    desc = bucket.desc
    coins = []

    for coin in bucket.coins:
        coins.append(coin)
        new_bucket = make_Bucket(desc, coins)
        if sufficient_funds([new_bucket], bucket_value_sum=new_bucket.value):
            return [new_bucket, ]
    return [bucket, ]


def make_Bucket(desc, coins):
    witness = any(Transaction.is_segwit_input(coin, guess_for_address=True) for coin in coins)
    # note that we're guessing whether the tx uses segwit based
    # on this single bucket
    weight = sum(Transaction.estimated_input_weight(coin, witness)
                 for coin in coins)
    value = sum(coin['value'] for coin in coins)
    min_height = min(coin['height'] for coin in coins)
    return Bucket(desc, weight, value, coins, min_height, witness)


class CoinChooserBase(PrintError):

    enable_output_value_rounding = False

    def keys(self, coins):
        raise NotImplementedError

    def bucketize_coins(self, coins):
        keys = self.keys(coins)
        buckets = defaultdict(list)
        for key, coin in zip(keys, coins):
            buckets[key].append(coin)
        return list(map(make_Bucket, buckets.keys(), buckets.values()))

    def penalty_func(self, tx):
        def penalty(candidate):
            return 0
        return penalty

    def change_amounts(self, tx, count, fee_estimator, dust_threshold):
        # Break change up if bigger than max_change
        output_amounts = [o.value for o in tx.outputs()]
        # Don't split change of less than 0.02 BTC
        max_change = max(max(output_amounts) * 1.25, 0.02 * COIN)

        # Use N change outputs
        for n in range(1, count + 1):
            # How much is left if we add this many change outputs?
            change_amount = max(0, tx.get_fee() - fee_estimator(n))
            if change_amount // n <= max_change:
                break

        # Get a handle on the precision of the output amounts; round our
        # change to look similar
        def trailing_zeroes(val):
            s = str(val)
            return len(s) - len(s.rstrip('0'))

        zeroes = [trailing_zeroes(i) for i in output_amounts]
        min_zeroes = min(zeroes)
        max_zeroes = max(zeroes)
        if n > 1:
            zeroes = range(max(0, min_zeroes - 1), (max_zeroes + 1) + 1)
        else:
            # if there is only one change output, this will ensure that we aim
            # to have one that is exactly as precise as the most precise output
            zeroes = [min_zeroes]

        # Calculate change; randomize it a bit if using more than 1 output
        remaining = change_amount
        amounts = []
        while n > 1:
            average = remaining / n
            amount = self.p.randint(int(average * 0.7), int(average * 1.3))
            precision = min(self.p.choice(zeroes), int(floor(log10(amount))))
            amount = int(round(amount, -precision))
            amounts.append(amount)
            remaining -= amount
            n -= 1

        # Last change output.  Round down to maximum precision but lose
        # no more than 100 satoshis to fees (2dp)
        # e.g. a max of 2 decimal places means losing 100 satoshis to fees
        max_dp_to_round_for_privacy = 2 if self.enable_output_value_rounding else 0
        N = pow(10, min(max_dp_to_round_for_privacy, zeroes[0]))
        amount = (remaining // N) * N
        amounts.append(amount)

        assert sum(amounts) <= change_amount

        return amounts

    def change_outputs(self, tx, change_addrs, fee_estimator, dust_threshold):
        amounts = self.change_amounts(tx, len(change_addrs), fee_estimator,
                                      dust_threshold)
        assert min(amounts) >= 0
        assert len(change_addrs) >= len(amounts)
        # If change is above dust threshold after accounting for the
        # size of the change output, add it to the transaction.
        dust = sum(amount for amount in amounts if amount < dust_threshold)
        amounts = [amount for amount in amounts if amount >= dust_threshold]
        change = [TxOutput(TYPE_ADDRESS, addr, amount)
                  for addr, amount in zip(change_addrs, amounts)]
        self.print_error('change:', change)
        if dust:
            self.print_error('not keeping dust', dust)
        return change

    def make_tx(self, coins, inputs, outputs, change_addrs, fee_estimator,
                dust_threshold, sender=None):
        """Select unspent coins to spend to pay outputs.  If the change is
        greater than dust_threshold (after adding the change output to
        the transaction) it is kept, otherwise none is sent and it is
        added to the transaction fee.

        Note: fee_estimator expects virtual bytes
        """

        # Deterministic randomness from coins
        utxos = [c['prevout_hash'] + str(c['prevout_n']) for c in coins]
        self.p = PRNG(''.join(sorted(utxos)))

        # Copy the ouputs so when adding change we don't modify "outputs"
        tx = Transaction.from_io(inputs[:], outputs[:])
        input_value = tx.input_value()
        # Weight of the transaction with no inputs and no change
        # Note: this will use legacy tx serialization as the need for "segwit"
        # would be detected from inputs. The only side effect should be that the
        # marker and flag are excluded, which is compensated in get_tx_weight()
        base_weight = tx.estimated_weight()
        spent_amount = tx.output_value()

        def fee_estimator_w(weight):
            return fee_estimator(Transaction.virtual_size_from_weight(weight))

        def get_tx_weight(buckets):
            total_weight = base_weight + sum(bucket.weight for bucket in buckets)
            is_segwit_tx = any(bucket.witness for bucket in buckets)
            if is_segwit_tx:
                total_weight += 2  # marker and flag
                # non-segwit inputs were previously assumed to have
                # a witness of '' instead of '00' (hex)
                # note that mixed legacy/segwit buckets are already ok
                num_legacy_inputs = sum((not bucket.witness) * len(bucket.coins)
                                        for bucket in buckets)
                total_weight += num_legacy_inputs

            return total_weight

        def sufficient_funds(buckets, *, bucket_value_sum):
            '''Given a list of buckets, return True if it has enough
            value to pay for the transaction'''
            # assert bucket_value_sum == sum(bucket.value for bucket in buckets)  # expensive!
            total_input = input_value + bucket_value_sum
            if total_input < spent_amount:  # shortcut for performance
                return False
            # note re performance: so far this was constant time
            # what follows is linear in len(buckets)
            total_weight = get_tx_weight(buckets)
            return total_input >= spent_amount + fee_estimator_w(total_weight)

        # Collect the coins into buckets, choose a subset of the buckets
        buckets = self.bucketize_coins(coins)
        buckets = self.choose_buckets(buckets, sufficient_funds,
                                      self.penalty_func(tx), sender)

        tx.add_inputs([coin for b in buckets for coin in b.coins])
        tx_weight = get_tx_weight(buckets)

        # change is sent back to sending address unless specified
        if not change_addrs:
            change_addrs = [tx.inputs()[0]['address']]
            # note: this is not necessarily the final "first input address"
            # because the inputs had not been sorted at this point
            assert is_address(change_addrs[0])

        # This takes a count of change outputs and returns a tx fee
        output_weight = 4 * Transaction.estimated_output_size(change_addrs[0])
        fee = lambda count: fee_estimator_w(tx_weight + count * output_weight)
        change = self.change_outputs(tx, change_addrs, fee, dust_threshold)
        tx.add_outputs(change)

        self.print_error("using %d inputs" % len(tx.inputs()))
        self.print_error("using buckets:", [bucket.desc for bucket in buckets])

        return tx

    def choose_buckets(self, buckets, sufficient_funds, penalty_func, sender=None):
        raise NotImplemented('To be subclassed')


class CoinChooserRandom(CoinChooserBase):

    def keys(self, coins):
        return [coin['address'] for coin in coins]

    def bucket_candidates_any(self, buckets, sufficient_funds):
        '''Returns a list of bucket sets.'''
        if not buckets:
            raise NotEnoughFunds()
        candidates = set()

        # Add all singletons
        for n, bucket in enumerate(buckets):
            if sufficient_funds([bucket], bucket_value_sum=bucket.value):
                candidates.add((n, ))

        # And now some random ones
        attempts = min(100, (len(buckets) - 1) * 10 + 1)
        permutation = list(range(len(buckets)))
        for i in range(attempts):
            # Get a random permutation of the buckets, and
            # incrementally combine buckets until sufficient
            self.p.shuffle(permutation)
            bkts = []
            bucket_value_sum = 0
            for count, index in enumerate(permutation):
                bucket = buckets[index]
                bkts.append(bucket)
                bucket_value_sum += bucket.value
                if sufficient_funds(bkts, bucket_value_sum=bucket_value_sum):
                    candidates.add(tuple(sorted(permutation[:count + 1])))
                    break
            else:
                raise NotEnoughFunds()

        candidates = [[buckets[n] for n in c] for c in candidates]
        return [strip_unneeded(c, sufficient_funds) for c in candidates]

    def bucket_candidates_prefer_confirmed(self, buckets, sufficient_funds):
        """Returns a list of bucket sets preferring confirmed coins.

        Any bucket can be:
        1. "confirmed" if it only contains confirmed coins; else
        2. "unconfirmed" if it does not contain coins with unconfirmed parents
        3. other: e.g. "unconfirmed parent" or "local"

        This method tries to only use buckets of type 1, and if the coins there
        are not enough, tries to use the next type but while also selecting
        all buckets of all previous types.
        """
        conf_buckets = [bkt for bkt in buckets if bkt.min_height > 0]
        unconf_buckets = [bkt for bkt in buckets if bkt.min_height == 0]
        other_buckets = [bkt for bkt in buckets if bkt.min_height < 0]

        bucket_sets = [conf_buckets, unconf_buckets, other_buckets]
        already_selected_buckets = []
        already_selected_buckets_value_sum = 0

        for bkts_choose_from in bucket_sets:
            try:
                def sfunds(bkts, *, bucket_value_sum):
                    bucket_value_sum += already_selected_buckets_value_sum
                    return sufficient_funds(already_selected_buckets + bkts,
                                            bucket_value_sum=bucket_value_sum)

                candidates = self.bucket_candidates_any(bkts_choose_from, sfunds)
                break
            except NotEnoughFunds:
                already_selected_buckets += bkts_choose_from
                already_selected_buckets_value_sum += sum(bucket.value for bucket in bkts_choose_from)
        else:
            raise NotEnoughFunds()

        candidates = [(already_selected_buckets + c) for c in candidates]
        return [strip_unneeded(c, sufficient_funds) for c in candidates]

    def choose_buckets(self, buckets, sufficient_funds, penalty_func, sender=None):
        candidates = self.bucket_candidates_prefer_confirmed(buckets, sufficient_funds)
        penalties = [penalty_func(cand) for cand in candidates]
        winner = candidates[penalties.index(min(penalties))]
        self.print_error("Bucket sets:", len(buckets))
        self.print_error("Winning penalty:", min(penalties))
        return winner


class CoinChooserPrivacy(CoinChooserRandom):
    """Attempts to better preserve user privacy.
    First, if any coin is spent from a user address, all coins are.
    Compared to spending from other addresses to make up an amount, this reduces
    information leakage about sender holdings.  It also helps to
    reduce blockchain UTXO bloat, and reduce future privacy loss that
    would come from reusing that address' remaining UTXOs.
    Second, it penalizes change that is quite different to the sent amount.
    Third, it penalizes change that is too big.
    """

    def keys(self, coins):
        return [coin['address'] for coin in coins]

    def penalty_func(self, tx):
        min_change = min(o.value for o in tx.outputs()) * 0.75
        max_change = max(o.value for o in tx.outputs()) * 1.33
        spent_amount = sum(o.value for o in tx.outputs())

        def penalty(buckets):
            badness = len(buckets) - 1
            total_input = sum(bucket.value for bucket in buckets)
            # FIXME "change" here also includes fees
            change = float(total_input - spent_amount)
            # Penalize change not roughly in output range
            if change < min_change:
                badness += (min_change - change) / (min_change + 10000)
            elif change > max_change:
                badness += (change - max_change) / (max_change + 10000)
                # Penalize large change; 5 BTC excess ~= using 1 more input
                badness += change / (COIN * 5)
            return badness

        return penalty


class CoinChooserOldestFirst(CoinChooserBase):
    '''Maximize transaction priority. Select the oldest unspent
    transaction outputs in your wallet, that are sufficient to cover
    the spent amount. Then, remove any unneeded inputs, starting with
    the smallest in value.
    '''
    def keys(self, coins):
        return [coin['prevout_hash'] + ':' + str(coin['prevout_n'])
                for coin in coins]

    def choose_buckets(self, buckets, sufficient_funds, penalty_func, sender=None):
        '''Spend the oldest buckets first.'''
        # Unconfirmed coins are young, not old
        adj_height = lambda height: 99999999 if height <= 0 else height
        buckets.sort(key = lambda b: max(adj_height(coin['height'])
                                         for coin in b.coins))
        selected = []
        bucket_value_sum = 0
        for bucket in buckets:
            selected.append(bucket)
            bucket_value_sum += bucket.value
            if sufficient_funds(selected, bucket_value_sum=bucket_value_sum):
                return strip_unneeded(selected, sufficient_funds)
        else:
            raise NotEnoughFunds()


class CoinChooserQtum(CoinChooserBase):
    def keys(self, coins):
        return [coin['address'] for coin in coins]

    def choose_buckets(self, buckets, sufficient_funds, penalty_func, sender=None):
        '''Spend the oldest buckets first.'''
        # Unconfirmed coins are young, not old
        adj_height = lambda height: 99999999 if height <= 0 else height
        buckets.sort(key=lambda b: max(adj_height(coin['height'])
                                       for coin in b.coins))
        selected = []
        bucket_value_sum = 0

        if sender:
            # put sender bucket to selected first
            for i in range(len(buckets)):
                bucket = buckets[i]
                if bucket.desc == sender:
                    del buckets[i]
                    selected.append(bucket)
                    bucket_value_sum += bucket.value
                    # check if it's already enough
                    if sufficient_funds(selected, bucket_value_sum=bucket_value_sum):
                        return strip_unneeded_utxo(selected, sufficient_funds)
                    break
            if len(selected) == 0:
                raise Exception('choose_buckets - sender address has no utxo')

        for bucket in buckets:
            selected.append(bucket)
            bucket_value_sum += bucket.value
            if sufficient_funds(selected, bucket_value_sum=bucket_value_sum):
                return strip_unneeded(selected, sufficient_funds, sender)
        else:
            raise NotEnoughFunds()


COIN_CHOOSERS = {'Privacy': CoinChooserPrivacy,
                 'OldestFirst': CoinChooserOldestFirst}


def get_name(config):
    kind = config.get('coin_chooser')
    if not kind in COIN_CHOOSERS:
        kind = 'Privacy'
    return kind


def get_coin_chooser(config):
    klass = COIN_CHOOSERS[get_name(config)]
    coinchooser = klass()
    coinchooser.enable_output_value_rounding = config.get('coin_chooser_output_rounding', False)
    return coinchooser
