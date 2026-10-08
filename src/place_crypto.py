"""Encryption at rest for a rider's saved places.

WHAT IS ACTUALLY SENSITIVE HERE, because that is what sets the bar. A saved
place is where somebody lives. "Home", a latitude and a longitude, next to an
email address and a phone number in one row is a dossier — and the rows it sits
in are the ones that end up in a database dump, a backup bucket, a read replica
a contractor can see, and whatever a SQL-injection bug reads. Every one of
those is a place the coordinates do not need to be legible.

So the blob is encrypted by the APPLICATION before it reaches Postgres, and the
column holds ciphertext. Volume encryption, which the host already provides,
protects a stolen disk and nothing else: the database serves plaintext to
anything holding a connection string, which is every one of the cases above.

WHAT THIS DOES NOT CLAIM. The server can decrypt — it has to, to serve a
rider's places back to a new device — so a live compromise of the running
process reads them, and so could we. This is encryption at rest, not
end-to-end, and the privacy policy says exactly that rather than implying a
guarantee the architecture does not make. The end-to-end version was considered
and declined by the owner: a rider who loses the key loses their places, and
there is no cross-device recovery without a passphrase flow nobody asked for.

FERNET, not something cleverer. AES-128-CBC with an HMAC-SHA256 tag and a
version byte, from `cryptography`, which is the boring well-reviewed answer and
is already how most of Python does this. The alternative was hand-rolling
AES-GCM, and hand-rolling is how nonce reuse happens.

THE KEY IS LOAD-BEARING AND REQUIRED, and follows `identity.py`'s rule for the
vehicle-identifier salt exactly — the same argument applies, so the same
discipline does. There is no dev fallback. A missing key is a startup error
rather than a silent degradation to plaintext, because a silent degradation to
plaintext is precisely the failure this module exists to prevent, and it would
be invisible: everything would keep working.

ROTATION is supported through `VEO_PLACES_KEY_OLD`, a second key tried only on
decrypt. Rotating means setting the new key as primary, leaving the old one in
place until everything has been re-saved, then dropping it. Without that door a
rotation would strand every stored place, which in practice means it would
never be done.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

log = logging.getLogger(__name__)

_ENV_VAR = "VEO_PLACES_KEY"
_ENV_VAR_OLD = "VEO_PLACES_KEY_OLD"

_GENERATE_HINT = (
    "Generate with `python3 -c 'from cryptography.fernet import Fernet; "
    "print(Fernet.generate_key().decode())'` and set it in your environment "
    "(GHA secret in prod, .env / shell locally). Losing the value strands "
    "every rider's saved places."
)


def _key(name: str) -> bytes | None:
    raw = os.environ.get(name)
    return raw.encode("utf-8") if raw else None


def _box() -> MultiFernet:
    """The cipher, primary key first.

    Built per call rather than cached at import: the tests set and clear the
    env between cases, and a module-level cipher would freeze whichever key
    happened to be set when this module was first imported — which is a bug
    that only ever shows up as "the wrong key in production".
    """
    primary = _key(_ENV_VAR)
    if not primary:
        raise RuntimeError(f"{_ENV_VAR} is required but unset. {_GENERATE_HINT}")
    keys = [Fernet(primary)]
    old = _key(_ENV_VAR_OLD)
    if old:
        # Decrypt-only in practice: `MultiFernet.encrypt` always uses the first
        # key, so adding an old one here never writes with it.
        keys.append(Fernet(old))
    return MultiFernet(keys)


def configured() -> bool:
    """Whether a key is set at all.

    For the one caller that has to degrade rather than fail: a GET of a profile
    whose places cannot be decrypted should still serve the rest of the
    profile. Writes never consult this — a write that cannot encrypt must fail
    loudly rather than store plaintext.
    """
    return bool(_key(_ENV_VAR))


def seal(places: Any) -> str:
    """JSON → ciphertext, as a str for a TEXT column.

    Raises when no key is set, on purpose. The alternative — returning the
    plaintext and carrying on — is the silent failure this module exists to
    prevent, and it would look exactly like success.
    """
    raw = json.dumps(places, separators=(",", ":"), ensure_ascii=False)
    return _box().encrypt(raw.encode("utf-8")).decode("ascii")


def unseal(blob: str | None) -> Any | None:
    """Ciphertext → JSON, or None.

    NONE FOR EVERY FAILURE, and the failures are not equivalent — so they are
    logged differently even though they return the same thing:

      * no blob is the ordinary case for a rider who has saved nothing;
      * an undecryptable blob means the key changed or the row was written by
        a key we no longer hold, which is an operational incident and says so;
      * a blob that decrypts to something that is not a list is a bug or
        tampering.

    It never raises, because a profile GET that 500s over one unreadable field
    takes the rider's email, phone and rate plan down with it — and those are
    the fields they need to fix the situation.
    """
    if not blob:
        return None
    try:
        raw = _box().decrypt(blob.encode("ascii"))
    except InvalidToken:
        log.error(
            "saved places could not be decrypted — wrong or rotated %s? "
            "Set %s to the previous key to recover.",
            _ENV_VAR,
            _ENV_VAR_OLD,
        )
        return None
    except RuntimeError:
        # No key configured. Already loud at startup for every write path; a
        # read degrades rather than taking the whole profile with it.
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.error("saved places decrypted to something that is not JSON")
        return None
