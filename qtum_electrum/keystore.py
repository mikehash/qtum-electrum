#!/usr/bin/env python2
# -*- mode: python -*-
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2016  The Electrum developers
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

from unicodedata import normalize
import hashlib
from typing import Tuple
from . import bitcoin, ecc, constants, bip32
from .qtum import (deserialize_privkey, serialize_privkey,
                   public_key_to_p2pkh)
from .bip32 import (bip32_public_derivation, deserialize_xpub, CKD_pub,
                    bip32_root, deserialize_xprv, bip32_private_derivation,
                    bip32_private_key, bip32_derivation, BIP32_PRIME,
                    is_xpub, is_xprv, CKD_priv)
from .ecc import string_to_number, number_to_string
from .crypto import pw_decode, pw_encode, sha256, sha256d, PW_HASH_VERSION_LATEST
from .util import (PrintError, InvalidPassword, WalletFileException,
                   QtumException, bh2u, bfh, print_error, inv_dict)
from .mnemonic import Mnemonic, load_wordlist, seed_type, is_seed


class KeyStore(PrintError):

    def has_seed(self):
        return False

    def is_watching_only(self):
        return False

    def can_import(self):
        return False

    def get_type_text(self) -> str:
        return f'{self.type}'

    def may_have_password(self):
        """Returns whether the keystore can be encrypted with a password."""
        raise NotImplementedError()

    def get_tx_derivations(self, tx):
        keypairs = {}
        for txin in tx.inputs():
            num_sig = txin.get('num_sig')
            if num_sig is None:
                continue
            x_signatures = txin['signatures']
            signatures = [sig for sig in x_signatures if sig]
            if len(signatures) == num_sig:
                # input is complete
                continue
            for k, x_pubkey in enumerate(txin['x_pubkeys']):
                if x_signatures[k] is not None:
                    # this pubkey already signed
                    continue
                derivation = self.get_pubkey_derivation(x_pubkey)
                if not derivation:
                    continue
                keypairs[x_pubkey] = derivation
        return keypairs

    def can_sign(self, tx):
        if self.is_watching_only():
            return False
        return bool(self.get_tx_derivations(tx))

    def ready_to_sign(self):
        return not self.is_watching_only()


class Software_KeyStore(KeyStore):

    def __init__(self, d):
        KeyStore.__init__(self)
        self.pw_hash_version = d.get('pw_hash_version', 1)

    def may_have_password(self):
        return not self.is_watching_only()

    def sign_message(self, sequence, message, password):
        privkey, compressed = self.get_private_key(sequence, password)
        key = ecc.ECPrivkey(privkey)
        return key.sign_message(message, compressed)

    def decrypt_message(self, sequence, message, password):
        privkey, compressed = self.get_private_key(sequence, password)
        ec = ecc.ECPrivkey(privkey)
        decrypted = ec.decrypt_message(message)
        return decrypted

    def sign_transaction(self, tx, password):
        if self.is_watching_only():
            return
        # Raise if password is not correct.
        self.check_password(password)
        # Add private keys
        keypairs = self.get_tx_derivations(tx)
        for k, v in keypairs.items():
            keypairs[k] = self.get_private_key(v, password)
        # Sign
        if keypairs:
            tx.sign(keypairs)

    def update_password(self, old_password, new_password):
        raise NotImplementedError()  # implemented by subclasses

    def check_password(self, password):
        raise NotImplementedError()  # implemented by subclasses


class Imported_KeyStore(Software_KeyStore):
    # keystore for imported private keys

    type = 'imported'

    def __init__(self, d):
        Software_KeyStore.__init__(self, d)
        self.keypairs = d.get('keypairs', {})

    def is_deterministic(self):
        return False

    def get_master_public_key(self):
        return None

    def dump(self):
        return {
            'type': self.type,
            'keypairs': self.keypairs,
            'pw_hash_version': self.pw_hash_version,
        }

    def can_import(self):
        return True

    def check_password(self, password):
        pubkey = list(self.keypairs.keys())[0]
        self.get_private_key(pubkey, password)

    def import_privkey(self, sec, password):
        txin_type, privkey, compressed = deserialize_privkey(sec)
        pubkey = ecc.ECPrivkey(privkey).get_public_key_hex(compressed=compressed)
        # re-serialize the key so the internal storage format is consistent
        serialized_privkey = serialize_privkey(
            privkey, compressed, txin_type, internal_use=True)
        self.keypairs[pubkey] = pw_encode(serialized_privkey, password, version=self.pw_hash_version)
        return txin_type, pubkey

    def delete_imported_key(self, key):
        self.keypairs.pop(key)

    def get_private_key(self, pubkey, password):
        sec = pw_decode(self.keypairs[pubkey], password, version=self.pw_hash_version)
        txin_type, privkey, compressed = deserialize_privkey(sec)
        # this checks the password
        if pubkey != ecc.ECPrivkey(privkey).get_public_key_hex(compressed=compressed):
            raise InvalidPassword()
        return privkey, compressed

    def get_pubkey_derivation(self, x_pubkey):
        if x_pubkey[0:2] in ['02', '03', '04']:
            if x_pubkey in self.keypairs.keys():
                return x_pubkey
        elif x_pubkey[0:2] == 'fd':
            addr = bitcoin.script_to_address(x_pubkey[2:])
            if addr in self.addresses:
                return self.addresses[addr].get('pubkey')
            
    def update_password(self, old_password, new_password):
        self.check_password(old_password)
        if new_password == '':
            new_password = None
        for k, v in self.keypairs.items():
            b = pw_decode(v, old_password, version=self.pw_hash_version)
            c = pw_encode(b, new_password, version=PW_HASH_VERSION_LATEST)
            self.keypairs[k] = c
        self.pw_hash_version = PW_HASH_VERSION_LATEST


class Deterministic_KeyStore(Software_KeyStore):

    def __init__(self, d):
        Software_KeyStore.__init__(self, d)
        self.seed = d.get('seed', '')
        self.passphrase = d.get('passphrase', '')

    def is_deterministic(self):
        return True

    def dump(self):
        d = {
            'type': self.type,
            'pw_hash_version': self.pw_hash_version,
        }
        if self.seed:
            d['seed'] = self.seed
        if self.passphrase:
            d['passphrase'] = self.passphrase
        return d

    def has_seed(self):
        return bool(self.seed)

    def is_watching_only(self):
        return not self.has_seed()

    def add_seed(self, seed):
        if self.seed:
            raise Exception("a seed exists")
        self.seed = self.format_seed(seed)

    def get_seed(self, password):
        return pw_decode(self.seed, password, version=self.pw_hash_version)

    def get_passphrase(self, password):
        if self.passphrase:
            return pw_decode(self.passphrase, password, version=self.pw_hash_version)
        else:
            return ''


class Xpub:

    def __init__(self):
        self.xpub = None
        self.xpub_receive = None
        self.xpub_change = None

    def get_master_public_key(self):
        return self.xpub

    def derive_pubkey(self, for_change, n):

        # m / 44'/ 88' / 0' / for_change / n
        xpub = self.xpub_change if for_change else self.xpub_receive
        if xpub is None:
            xpub = bip32_public_derivation(self.xpub, "", "/%d" % for_change)
            if for_change:
                self.xpub_change = xpub
            else:
                self.xpub_receive = xpub
        return self.get_pubkey_from_xpub(xpub, (n,))

    @classmethod
    def get_pubkey_from_xpub(cls, xpub, sequence):
        _, _, _, _, c, cK = deserialize_xpub(xpub)
        for i in sequence:
            cK, c = CKD_pub(cK, c, i)
        public_key = bh2u(cK)
        return public_key

    def get_xpubkey(self, c, i):
        s = ''.join(map(lambda x: bitcoin.int_to_hex(x,2), (c, i)))
        return 'ff' + bh2u(bitcoin.DecodeBase58Check(self.xpub)) + s


    @classmethod
    def parse_xpubkey(self, pubkey):
        assert pubkey[0:2] == 'ff'
        pk = bfh(pubkey)
        pk = pk[1:]
        xkey = bitcoin.EncodeBase58Check(pk[0:78])
        dd = pk[78:]
        s = []
        while dd:
            n = int(bitcoin.rev_hex(bh2u(dd[0:2])), 16)
            dd = dd[2:]
            s.append(n)
        assert len(s) == 2
        return xkey, s

    def get_pubkey_derivation(self, x_pubkey):
        if x_pubkey[0:2] != 'ff':
            return
        xpub, derivation = self.parse_xpubkey(x_pubkey)
        if self.xpub != xpub:
            return
        return derivation


class BIP32_KeyStore(Deterministic_KeyStore, Xpub):

    type = 'bip32'

    def __init__(self, d):
        Xpub.__init__(self)
        Deterministic_KeyStore.__init__(self, d)
        self.xpub = d.get('xpub')
        self.xprv = d.get('xprv')
        self.derivation = d.get('derivation', '')

    def format_seed(self, seed):
        return ' '.join(seed.split())

    def dump(self):
        d = Deterministic_KeyStore.dump(self)
        d['xpub'] = self.xpub
        d['xprv'] = self.xprv
        d['derivation'] = self.derivation
        return d

    def get_master_private_key(self, password):
        return pw_decode(self.xprv, password, version=self.pw_hash_version)

    def check_password(self, password):
        xprv = pw_decode(self.xprv, password, version=self.pw_hash_version)
        if deserialize_xprv(xprv)[4] != deserialize_xpub(self.xpub)[4]:
            raise InvalidPassword()

    def update_password(self, old_password, new_password):
        self.check_password(old_password)
        if new_password == '':
            new_password = None
        if self.has_seed():
            decoded = self.get_seed(old_password)
            self.seed = pw_encode(decoded, new_password, version=PW_HASH_VERSION_LATEST)
        if self.passphrase:
            decoded = self.get_passphrase(old_password)
            self.passphrase = pw_encode(decoded, new_password, version=PW_HASH_VERSION_LATEST)
        if self.xprv is not None:
            b = pw_decode(self.xprv, old_password, version=PW_HASH_VERSION_LATEST)
            self.xprv = pw_encode(b, new_password, version=PW_HASH_VERSION_LATEST)
        self.pw_hash_version = PW_HASH_VERSION_LATEST

    def is_watching_only(self):
        return self.xprv is None

    def add_xprv(self, xprv):
        self.xprv = xprv
        self.xpub = bip32.xpub_from_xprv(xprv)

    def add_xprv_from_seed(self, bip32_seed, xtype, derivation):
        xprv, xpub = bip32_root(bip32_seed, xtype)
        xprv, xpub = bip32_private_derivation(xprv, "m/", derivation)
        self.derivation = derivation
        self.add_xprv(xprv)

    def get_private_key(self, sequence, password):
        xprv = self.get_master_private_key(password)
        _, _, _, _, c, k = deserialize_xprv(xprv)
        pk = bip32_private_key(sequence, k, c)
        return pk, True


class Mobile_KeyStore(BIP32_KeyStore):

    type = 'mobile'

    def __init__(self, d):
        BIP32_KeyStore.__init__(self, d)
        self.keypairs = d.get('keypairs', {})

    def dump(self):
        d = Deterministic_KeyStore.dump(self)
        d['type'] = self.type
        d['xpub'] = self.xpub
        d['xprv'] = self.xprv
        d['derivation'] = mobile_derivation()
        d['keypairs'] = self.keypairs
        return d

    def can_import(self):
        return False

    def derive_pubkey(self, for_change, n):
        master_xprv = self.get_master_private_key(None)
        sub_xprv, sub_xpub = bip32_private_derivation(master_xprv, "", "/{}'".format(n))
        return self.get_pubkey_from_xpub(sub_xpub, ())

    @classmethod
    def get_privatekey_from_xprv(cls, xprv, sequence):
        _, _, _, _, c, cK = deserialize_xprv(xprv)
        for i in sequence:
            cK, c = CKD_priv(cK, c, i)
        return cK

    def derive_privkey(self, sequence, password):
        master_xprv = self.get_master_private_key(password)
        sub_xprv, sub_xpub = bip32_private_derivation(master_xprv, "", "/{}'".format(sequence[1]))
        pk = self.get_privatekey_from_xprv(sub_xprv, ())
        return pk, True

    def get_private_key(self, pubkey, password):
        sec = pw_decode(self.keypairs[pubkey], password, version=self.pw_hash_version)
        txin_type, privkey, compressed = deserialize_privkey(sec)
        # this checks the password
        if pubkey != ecc.ECPrivkey(privkey).get_public_key_hex(compressed=compressed):
            raise InvalidPassword()
        return privkey, compressed

    def import_privkey(self, sec, password):
        txin_type, privkey, compressed = deserialize_privkey(sec)
        pubkey = ecc.ECPrivkey(privkey).get_public_key_hex(compressed=compressed)
        # re-serialize the key so the internal storage format is consistent
        serialized_privkey = serialize_privkey(
            privkey, compressed, txin_type, internal_use=True)
        self.keypairs[pubkey] = pw_encode(serialized_privkey, password, version=self.pw_hash_version)
        return txin_type, pubkey

    def get_pubkey_derivation(self, x_pubkey):
        if x_pubkey[0:2] in ['02', '03', '04']:
            if x_pubkey in self.keypairs.keys():
                return x_pubkey
        elif x_pubkey[0:2] == 'fd':
            addr = bitcoin.script_to_address(x_pubkey[2:])
            if addr in self.addresses:
                return self.addresses[addr].get('pubkey')


class Qt_Core_Keystore(BIP32_KeyStore):

    type = 'qtcore'

    def __init__(self, d):
        BIP32_KeyStore.__init__(self, d)
        self.ext_master_xprv = d.get('ext_master_xprv', '')

    def dump(self):
        d = Deterministic_KeyStore.dump(self)
        d['type'] = self.type
        d['xpub'] = self.xpub
        d['xprv'] = self.xprv
        d['derivation'] = qt_core_derivation()
        d['ext_master_xprv'] = self.ext_master_xprv
        return d

    def derive_pubkey(self, for_change, n):
        master_xprv = self.get_master_private_key(None)
        sub_xprv, sub_xpub = bip32_private_derivation(master_xprv, "", "/{}'".format(n))
        return self.get_pubkey_from_xpub(sub_xpub, ())

    @classmethod
    def get_privatekey_from_xprv(cls, xprv, sequence):
        _, _, _, _, c, cK = deserialize_xprv(xprv)
        for i in sequence:
            cK, c = CKD_priv(cK, c, i)
        return cK

    def get_private_key(self, sequence, password):
        master_xprv = self.get_master_private_key(password)
        sub_xprv, sub_xpub = bip32_private_derivation(master_xprv, "", "/{}'".format(sequence[1]))
        pk = self.get_privatekey_from_xprv(sub_xprv, ())
        return pk, True


class Old_KeyStore(Deterministic_KeyStore):

    type = 'old'

    def __init__(self, d):
        Deterministic_KeyStore.__init__(self, d)
        self.mpk = d.get('mpk')

    def get_hex_seed(self, password):
        return pw_decode(self.seed, password, version=self.pw_hash_version).encode('utf8')

    def dump(self):
        d = Deterministic_KeyStore.dump(self)
        d['mpk'] = self.mpk
        return d

    def add_seed(self, seedphrase):
        Deterministic_KeyStore.add_seed(self, seedphrase)
        s = self.get_hex_seed(None)
        self.mpk = self.mpk_from_seed(s)

    def add_master_public_key(self, mpk):
        self.mpk = mpk

    def format_seed(self, seed):
        from . import old_mnemonic
        # see if seed was entered as hex
        seed = seed.strip()
        if seed:
            try:
                bfh(seed)
                return str(seed)
            except Exception:
                pass
        words = seed.split()
        seed = old_mnemonic.mn_decode(words)
        if not seed:
            raise Exception("Invalid seed")
        return seed

    def get_seed(self, password):
        from . import old_mnemonic
        s = self.get_hex_seed(password)
        return ' '.join(old_mnemonic.mn_encode(s))

    @classmethod
    def mpk_from_seed(klass, seed):
        secexp = klass.stretch_key(seed)
        privkey = ecc.ECPrivkey.from_secret_scalar(secexp)
        return privkey.get_public_key_hex(compressed=False)[2:]

    @classmethod
    def stretch_key(self, seed):
        x = seed
        for i in range(100000):
            x = hashlib.sha256(x + seed).digest()
        return string_to_number(x)

    @classmethod
    def get_sequence(self, mpk, for_change, n):
        return string_to_number(sha256d(("%d:%d:"%(n, for_change)).encode('ascii') + bfh(mpk)))


    @classmethod
    def get_pubkey_from_mpk(self, mpk, for_change, n):
        z = self.get_sequence(mpk, for_change, n)
        master_public_key = ecc.ECPubkey(bfh('04' + mpk))
        public_key = master_public_key + z * ecc.generator()
        return public_key.get_public_key_hex(compressed=False)

    def derive_pubkey(self, for_change, n):
        return self.get_pubkey_from_mpk(self.mpk, for_change, n)

    def get_private_key_from_stretched_exponent(self, for_change, n, secexp):
        secexp = (secexp + self.get_sequence(self.mpk, for_change, n)) % ecc.CURVE_ORDER
        pk = number_to_string(secexp, ecc.CURVE_ORDER)
        return pk

    def get_private_key(self, sequence, password):
        seed = self.get_hex_seed(password)
        self.check_seed(seed)
        for_change, n = sequence
        secexp = self.stretch_key(seed)
        pk = self.get_private_key_from_stretched_exponent(for_change, n, secexp)
        return pk, False

    def check_seed(self, seed):
        secexp = self.stretch_key(seed)
        master_private_key = ecc.ECPrivkey.from_secret_scalar(secexp)
        master_public_key = master_private_key.get_public_key_bytes(compressed=False)[1:]
        if master_public_key != bfh(self.mpk):
            print_error('invalid password (mpk)', self.mpk, bh2u(master_public_key))
            raise InvalidPassword()

    def check_password(self, password):
        seed = self.get_hex_seed(password)
        self.check_seed(seed)

    def get_master_public_key(self):
        return self.mpk

    def get_xpubkey(self, for_change, n):
        s = ''.join(map(lambda x: bitcoin.int_to_hex(x,2), (for_change, n)))
        return 'fe' + self.mpk + s

    @classmethod
    def parse_xpubkey(self, x_pubkey):
        assert x_pubkey[0:2] == 'fe'
        pk = x_pubkey[2:]
        mpk = pk[0:128]
        dd = pk[128:]
        s = []
        while dd:
            n = int(bitcoin.rev_hex(dd[0:4]), 16)
            dd = dd[4:]
            s.append(n)
        assert len(s) == 2
        return mpk, s

    def get_pubkey_derivation(self, x_pubkey):
        if x_pubkey[0:2] != 'fe':
            return
        mpk, derivation = self.parse_xpubkey(x_pubkey)
        if self.mpk != mpk:
            return
        return derivation

    def update_password(self, old_password, new_password):
        self.check_password(old_password)
        if new_password == '':
            new_password = None
        if self.has_seed():
            decoded = pw_decode(self.seed, old_password, version=self.pw_hash_version)
            self.seed = pw_encode(decoded, new_password, version=self.pw_hash_version)
        self.pw_hash_version = PW_HASH_VERSION_LATEST


class Hardware_KeyStore(KeyStore, Xpub):
    # Derived classes must set:
    #   - device
    #   - DEVICE_IDS
    #   - wallet_type

    type = 'hardware'

    def __init__(self, d):
        Xpub.__init__(self)
        KeyStore.__init__(self)
        # Errors and other user interaction is done through the wallet's
        # handler.  The handler is per-window and preserved across
        # device reconnects
        self.xpub = d.get('xpub')
        self.label = d.get('label')
        self.derivation = d.get('derivation')
        self.handler = None

    def set_label(self, label):
        self.label = label

    def may_have_password(self):
        return False

    def is_deterministic(self):
        return True

    def dump(self):
        return {
            'type': self.type,
            'hw_type': self.hw_type,
            'xpub': self.xpub,
            'derivation':self.derivation,
            'label':self.label,
        }

    def unpaired(self):
        '''A device paired with the wallet was diconnected.  This can be
        called in any thread context.'''
        self.print_error("unpaired")

    def paired(self):
        '''A device paired with the wallet was (re-)connected.  This can be
        called in any thread context.'''
        self.print_error("paired")

    def can_export(self):
        return False

    def is_watching_only(self):
        '''The wallet is not watching-only; the user will be prompted for
        pin and passphrase as appropriate when needed.'''
        assert not self.has_seed()
        return False

    def get_password_for_storage_encryption(self):
        from .storage import get_derivation_used_for_hw_device_encryption
        client = self.plugin.get_client(self)
        derivation = get_derivation_used_for_hw_device_encryption()
        xpub = client.get_xpub(derivation, "standard")
        password = self.get_pubkey_from_xpub(xpub, ())
        return password

    def has_usable_connection_with_device(self):
        if not hasattr(self, 'plugin'):
            return False
        client = self.plugin.get_client(self, force_pair=False)
        if client is None:
            return False
        return client.has_usable_connection_with_device()

    def ready_to_sign(self):
        return super().ready_to_sign() and self.has_usable_connection_with_device()


def bip39_normalize_passphrase(passphrase):
    return normalize('NFKD', passphrase or '')


def bip39_to_seed(mnemonic, passphrase):
    import hashlib, hmac
    PBKDF2_ROUNDS = 2048
    mnemonic = normalize('NFKD', ' '.join(mnemonic.split()))
    passphrase = bip39_normalize_passphrase(passphrase)
    return hashlib.pbkdf2_hmac('sha512', mnemonic.encode('utf-8'),
        b'mnemonic' + passphrase.encode('utf-8'), iterations=PBKDF2_ROUNDS)


# returns tuple (is_checksum_valid, is_wordlist_valid)
def bip39_is_checksum_valid(mnemonic: str) -> Tuple[bool, bool]:
    """Test checksum of bip39 mnemonic assuming English wordlist.
    Returns tuple (is_checksum_valid, is_wordlist_valid)
    """
    words = [ normalize('NFKD', word) for word in mnemonic.split() ]
    words_len = len(words)
    wordlist = load_wordlist("english.txt")
    n = len(wordlist)
    i = 0
    words.reverse()
    while words:
        w = words.pop()
        try:
            k = wordlist.index(w)
        except ValueError:
            return False, False
        i = i*n + k
    if words_len not in [12, 15, 18, 21, 24]:
        return False, True
    checksum_length = 11 * words_len // 33  # num bits
    entropy_length = 32 * checksum_length  # num bits
    entropy = i >> checksum_length
    checksum = i % 2**checksum_length
    entropy_bytes = int.to_bytes(entropy, length=entropy_length//8, byteorder="big")
    hashed = int.from_bytes(sha256(entropy_bytes), byteorder="big")
    calculated_checksum = hashed >> (256 - checksum_length)
    return checksum == calculated_checksum, True


def xtype_from_derivation(derivation):
    """Returns the script type to be used for this derivation."""
    # not sure if qtum uses 84 and 49 or not
    if derivation.startswith("m/84'"):
        return 'p2wpkh'
    elif derivation.startswith("m/49'"):
        return 'p2wpkh-p2sh'
    elif derivation.startswith("m/44'"):
        return 'standard'
    elif derivation.startswith("m/45'"):
        return 'standard'

    bip32_indices = list(bip32_derivation(derivation))
    if len(bip32_indices) >= 4:
        if bip32_indices[0] == 48 + BIP32_PRIME:
            # m / purpose' / coin_type' / account' / script_type' / change / address_index
            script_type_int = bip32_indices[3] - BIP32_PRIME
            script_type = PURPOSE48_SCRIPT_TYPES_INV.get(script_type_int)
            if script_type is not None:
                return script_type
    return 'standard'

# extended pubkeys
def is_xpubkey(x_pubkey):
    return x_pubkey[0:2] == 'ff'


def parse_xpubkey(x_pubkey):
    assert x_pubkey[0:2] == 'ff'
    return BIP32_KeyStore.parse_xpubkey(x_pubkey)


def xpubkey_to_address(x_pubkey):
    if x_pubkey[0:2] == 'fd':
        # TODO: check that ord() is OK here
        addrtype = ord(bfh(x_pubkey[2:4]))
        hash160 = bfh(x_pubkey[4:])
        address = bitcoin.hash160_to_b58_address(hash160, addrtype)
        return x_pubkey, address
    if x_pubkey[0:2] in ['02', '03', '04']:
        pubkey = x_pubkey
    elif x_pubkey[0:2] == 'ff':
        xpub, s = BIP32_KeyStore.parse_xpubkey(x_pubkey)
        pubkey = BIP32_KeyStore.get_pubkey_from_xpub(xpub, s)
    elif x_pubkey[0:2] == 'fe':
        mpk, s = Old_KeyStore.parse_xpubkey(x_pubkey)
        pubkey = Old_KeyStore.get_pubkey_from_mpk(mpk, s[0], s[1])
    else:
        raise QtumException("Cannot parse pubkey. prefix: {}"
                            .format(x_pubkey[0:2]))
    if pubkey:
        address = public_key_to_p2pkh(bfh(pubkey))
    return pubkey, address


def xpubkey_to_pubkey(x_pubkey):
    pubkey, address = xpubkey_to_address(x_pubkey)
    return pubkey

hw_keystores = {}


def register_keystore(hw_type, constructor):
    hw_keystores[hw_type] = constructor


def hardware_keystore(d):
    hw_type = d['hw_type']
    if hw_type in hw_keystores:
        constructor = hw_keystores[hw_type]
        return constructor(d)
    raise WalletFileException(f'unknown hardware type: {hw_type}. '
                              f'hw_keystores: {list(hw_keystores)}')


def is_old_mpk(mpk):
    try:
        int(mpk, 16)
    except:
        return False
    return len(mpk) == 128


def is_address_list(text):
    parts = text.split()
    return bool(parts) and all(bitcoin.is_address(x) for x in parts)


def get_private_keys(text, *, allow_spaces_inside_key=True):
    if allow_spaces_inside_key:  # see #1612
        parts = text.split('\n')
        parts = map(lambda x: ''.join(x.split()), parts)
        parts = list(filter(bool, parts))
    else:
        parts = text.split()
    if bool(parts) and all(bitcoin.is_private_key(x) for x in parts):
        return parts


def is_private_key_list(text, *, allow_spaces_inside_key=True):
    return bool(get_private_keys(text, allow_spaces_inside_key=allow_spaces_inside_key))


is_mpk = lambda x: is_old_mpk(x) or is_xpub(x)
is_private = lambda x: is_seed(x) or is_xprv(x) or is_private_key_list(x)
is_master_key = lambda x: is_old_mpk(x) or is_xprv(x) or is_xpub(x)
is_private_key = lambda x: is_xprv(x) or is_private_key_list(x)
is_bip32_key = lambda x: is_xprv(x) or is_xpub(x)


def purpose48_derivation(account_id: int, xtype: str) -> str:
    # m / purpose' / coin_type' / account' / script_type' / change / address_index
    bip43_purpose = 48
    coin = constants.net.BIP44_COIN_TYPE
    account_id = int(account_id)
    script_type_int = PURPOSE48_SCRIPT_TYPES.get(xtype)
    if script_type_int is None:
        raise Exception('unknown xtype: {}'.format(xtype))
    return "m/%d'/%d'/%d'/%d'" % (bip43_purpose, coin, account_id, script_type_int)


def bip44_derivation(account_id, bip43_purpose=44, coin=None):
    if coin is None:
        coin = constants.net.BIP44_COIN_TYPE
    return "m/%d'/%d'/%d'" % (bip43_purpose, coin, int(account_id))


def qt_core_derivation():
    return "m/0'/0'"


def mobile_derivation():
    return "m/88'/0'"


def from_private_key_list(text):
    keystore = Imported_KeyStore({})
    for x in get_private_keys(text):
        keystore.import_privkey(x, None)
    return keystore


def load_keystore(storage, name):
    d = storage.get(name, {})
    t = d.get('type')
    if not t:
        raise WalletFileException(
            'Wallet format requires update.\n'
            'Cannot find keystore for name {}'.format(name))
    keystore_constructors = {ks.type: ks for ks in
                             [Old_KeyStore, Imported_KeyStore, BIP32_KeyStore, Mobile_KeyStore, Qt_Core_Keystore]}
    keystore_constructors['hardware'] = hardware_keystore
    try:
        ks_constructor = keystore_constructors[t]
    except KeyError:
        raise WalletFileException(f'Unknown type {t} for keystore named {name}')
    k = ks_constructor(d)
    return k


def from_seed(seed, passphrase, is_p2sh=True):
    t = seed_type(seed)
    if t in ['standard', 'segwit']:
        if t == 'segwit':
            if is_p2sh:
                derivarion = bip44_derivation(0, bip43_purpose=49)
            else:
                derivarion = bip44_derivation(0, bip43_purpose=84)
        else:
            derivarion = bip44_derivation(0, bip43_purpose=44)
        keystore = from_bip39_seed(seed, passphrase, derivarion)
        keystore.add_seed(seed)
        keystore.passphrase = passphrase
        return keystore
    else:
        raise QtumException('Unexpected seed type {}'.format(t))


def from_bip39_seed(seed, passphrase, derivation, xtype=None):
    k = BIP32_KeyStore({})
    bip32_seed = bip39_to_seed(seed, passphrase)
    if xtype is None:
        xtype = xtype_from_derivation(derivation)
    k.add_xprv_from_seed(bip32_seed, xtype, derivation)
    return k


PURPOSE48_SCRIPT_TYPES = {
    'p2wsh-p2sh': 1,  # specifically multisig
    'p2wsh': 2,       # specifically multisig
}
PURPOSE48_SCRIPT_TYPES_INV = inv_dict(PURPOSE48_SCRIPT_TYPES)


def from_mobile_seed(seed):
    passphrase = ''
    bip32_seed = Mnemonic.mnemonic_to_seed(seed, passphrase)
    k = Mobile_KeyStore({})
    k.add_seed(seed)
    k.passphrase = passphrase
    k.add_xprv_from_seed(bip32_seed, 'standard', mobile_derivation())
    return k


def from_old_mpk(mpk):
    keystore = Old_KeyStore({})
    keystore.add_master_public_key(mpk)
    return keystore


def from_xpub(xpub):
    k = BIP32_KeyStore({})
    k.xpub = xpub
    return k


def from_xprv(xprv):
    xpub = bip32.xpub_from_xprv(xprv)
    k = BIP32_KeyStore({})
    k.xprv = xprv
    k.xpub = xpub
    return k


def from_master_key(text):
    if is_xprv(text):
        k = from_xprv(text)
    elif is_old_mpk(text):
        k = from_old_mpk(text)
    elif is_xpub(text):
        k = from_xpub(text)
    else:
        raise QtumException('Invalid key')
    return k


def from_qt_core_xprv(ext_master_xprv):
    k = Qt_Core_Keystore({})
    k.ext_master_xprv = ext_master_xprv
    xprv, xpub = bip32_private_derivation(ext_master_xprv, "m/", qt_core_derivation())
    k.add_xprv(xprv)
    return k


def from_qt_core_xpub(xpub):
    k = Qt_Core_Keystore({})
    k.xpub = xpub
    return k


def from_qt_core_master_key(text):
    if is_xprv(text):
        k = from_qt_core_xprv(text)
    # not support yet
    # elif is_xpub(text):
    #     k = from_desktop_xpub(text)
    else:
        raise Exception('Invalid key')
    return k
