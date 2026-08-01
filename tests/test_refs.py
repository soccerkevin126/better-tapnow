import pytest

from tapnow.refs import RefError, render_template, resolve_ref

CTX = {"plan": {"scenes": [{"image_prompt": "a cat"}, {"image_prompt": "a dog"}]},
       "critique": "too dark"}


def test_resolve_nested_and_indexed():
    assert resolve_ref("plan.scenes[1].image_prompt", CTX) == "a dog"
    assert resolve_ref("critique", CTX) == "too dark"


def test_resolve_missing_key_raises():
    with pytest.raises(RefError, match="'nope' not found"):
        resolve_ref("plan.nope", CTX)


def test_render_template_strings_and_json():
    out = render_template("Fix: {{ critique }}. First: {{ plan.scenes[0].image_prompt }}", CTX)
    assert out == "Fix: too dark. First: a cat"
    assert '"image_prompt": "a cat"' in render_template("{{ plan.scenes }}", CTX)


def test_resolve_attribute_access():
    class Thing:
        name = "brief.md"
    assert resolve_ref("f.name", {"f": Thing()}) == "brief.md"
