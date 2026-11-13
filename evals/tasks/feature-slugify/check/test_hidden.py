import pytest
from text import slugify, truncate


@pytest.mark.parametrize(
    "title, slug",
    [
        ("Hello, World!", "hello-world"),
        ("  Multiple   spaces  ", "multiple-spaces"),
        ("a--b__c", "a-b-c"),
        ("Café au lait", "café-au-lait"),
        ("中文 标题", "中文-标题"),
        ("Version 2.0", "version-2-0"),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_slugify(title, slug):
    assert slugify(title) == slug


def test_truncate_still_works():
    assert truncate("abcdef", 4) == "abc…"
    assert truncate("abc", 4) == "abc"
