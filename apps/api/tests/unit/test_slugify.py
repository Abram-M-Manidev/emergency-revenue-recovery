from app.shared.utils.slugify import slugify


def test_slugify_lowercases_and_dashes():
    assert slugify("Acme HVAC & Plumbing") == "acme-hvac-plumbing"


def test_slugify_strips_leading_trailing_dashes():
    assert slugify("  --Acme--  ") == "acme"


def test_slugify_falls_back_for_empty_input():
    assert slugify("!!!") != ""
