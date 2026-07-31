"""
ARK identifier utilities — minting, parsing, and NOID check-digit validation.

ARK format used by this project: ark:/87287/d7[noid]
NAAN: 87287 (UC Davis Library)
Shoulder: d7
"""

from digtk.lib.config import CONFIG

NAAN = CONFIG["ark"]["naan"]
SHOULDER = CONFIG["ark"]["shoulder"]
NOID_ALPHABET = CONFIG["ark"]["noid_alphabet"]


def mint(opts={}):
    """Mint a new ARK identifier with a NOID check digit.

    :param dict opts: Options. Accepts 'counter' (int) as the basis for the NOID.
    :returns: Full ARK string (e.g. 'ark:/87287/d7abc1234').
    :rtype: str
    """
    raise NotImplementedError("phase 0.2")


def parse(ark):
    """Parse an ARK string into its components.

    :param str ark: ARK string (e.g. 'ark:/87287/d7abc1234').
    :returns: Dict with keys 'naan', 'shoulder', 'noid'.
    :rtype: dict
    :raises ValueError: If the ARK does not match the expected format.
    """
    raise NotImplementedError("phase 0.2")


def check_digit(noid):
    """Compute the NOID check digit for a given identifier string.

    :param str noid: NOID string (without check digit).
    :returns: Single check-digit character.
    :rtype: str
    """
    raise NotImplementedError("phase 0.2")


def validate(ark):
    """Validate an ARK string including its NOID check digit.

    :param str ark: ARK string to validate.
    :returns: True if the ARK is structurally valid and the check digit matches.
    :rtype: bool
    """
    raise NotImplementedError("phase 0.2")
