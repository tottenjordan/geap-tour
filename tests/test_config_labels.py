"""RESOURCE_LABELS is the single source for the default resource label.

Every GCP resource we create is stamped with these labels so demo assets are
filterable/attributable. Python creation sites import the dict; shell/gcloud
sites use the formatters (gcloud comma=key=value, bq colon key:value).
"""

import src.config as cfg


def test_resource_labels_default():
    assert cfg.RESOURCE_LABELS == {"solution": "geap-tour"}


def test_resource_labels_gcloud_format():
    assert cfg.resource_labels_gcloud() == "solution=geap-tour"


def test_resource_labels_bq_flags():
    assert cfg.resource_labels_bq_flags() == ["--label", "solution:geap-tour"]
