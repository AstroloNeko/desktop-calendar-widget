"""Session-only selection of stable Event IDs in a visible workspace context."""


class EventSelection:
    def __init__(self):
        self.ids: set[str] = set()
        self.anchor = None
        self.context = None

    def clear(self):
        self.ids.clear()
        self.anchor = None

    def reconcile(self, visible_ids, context):
        visible = set(visible_ids)
        if context != self.context:
            self.clear()
            self.context = context
        self.ids.intersection_update(visible)
        if self.anchor not in visible:
            self.anchor = None

    def single(self, identifier, visible_ids):
        self.ids = {identifier} if identifier in set(visible_ids) else set()
        self.anchor = identifier if self.ids else None

    def toggle(self, identifier, visible_ids):
        if identifier not in set(visible_ids):
            return
        if identifier in self.ids:
            self.ids.remove(identifier)
        else:
            self.ids.add(identifier)
        self.anchor = identifier

    def range_to(self, identifier, ordered_ids, *, additive=False):
        order = tuple(dict.fromkeys(ordered_ids))
        if identifier not in order:
            return
        anchor = self.anchor if self.anchor in order else identifier
        left, right = sorted((order.index(anchor), order.index(identifier)))
        selected = set(order[left:right+1])
        self.ids = self.ids | selected if additive else selected
        self.anchor = anchor
