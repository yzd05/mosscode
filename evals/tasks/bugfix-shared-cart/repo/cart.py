class Cart:
    def __init__(self, items=[]):
        self.items = items

    def add(self, name, price, quantity=1):
        self.items.append((name, price, quantity))

    def total(self):
        return sum(price * quantity for _, price, quantity in self.items)
