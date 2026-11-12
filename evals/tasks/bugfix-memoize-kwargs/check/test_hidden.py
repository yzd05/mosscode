from memo import memoize


def scaler():
    calls = []

    @memoize
    def scale(x, factor=1):
        calls.append((x, factor))
        return x * factor

    return scale, calls


def test_keyword_arguments_are_part_of_the_key():
    scale, _ = scaler()
    assert scale(3) == 3
    assert scale(3, factor=2) == 6
    assert scale(3, factor=5) == 15


def test_repeated_calls_are_cached():
    scale, calls = scaler()
    scale(2, factor=3)
    scale(2, factor=3)
    scale(2)
    scale(2)
    assert calls == [(2, 3), (2, 1)]


def test_keyword_order_does_not_matter():
    calls = []

    @memoize
    def area(*, width, height):
        calls.append(1)
        return width * height

    assert area(width=2, height=3) == area(height=3, width=2) == 6
    assert len(calls) == 1


def test_keeps_the_name():
    assert scaler()[0].__name__ == "scale"
