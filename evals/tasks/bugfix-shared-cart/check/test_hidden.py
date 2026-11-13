from cart import Cart


def test_new_carts_are_independent():
    first, second = Cart(), Cart()
    first.add("pen", 2.5)
    assert second.items == [] and second.total() == 0
    assert first.total() == 2.5


def test_given_items_are_not_changed():
    start = [("book", 10.0, 1)]
    cart = Cart(start)
    cart.add("pen", 2.0, 2)
    assert start == [("book", 10.0, 1)]
    assert cart.total() == 14.0
