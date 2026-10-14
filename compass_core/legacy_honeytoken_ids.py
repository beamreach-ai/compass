"""Pre-rename honeytoken class ids.

Kept in one place, outside the honeypot feature tree, so the retired
vocabulary (which the feature's CI guard forbids) lives only here. Used by
captains.honeypot.classes.normalize_class.
"""
from __future__ import annotations

LEGACY_CLASS_ALIASES = {
    "iam_canary": "aws_credential",
    "s3_canary": "s3_object",
    "dns_canary": "dns_record",
}
