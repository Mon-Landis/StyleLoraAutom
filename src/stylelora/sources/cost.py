import asyncio
from stylelora.backends.types import AccountStatus, BackendError
from stylelora.project_db import commands, read_snapshot
from stylelora.project_db.repositories import sources
from stylelora.project_db.types import ItemFilter, PageRequest
from stylelora.project_db.validation import now


def all_items(project, source_id, statuses=()):
    result, after = [], 0
    with read_snapshot(project) as db:
        while True:
            page = sources.list_items(db, ItemFilter(source_id, statuses), PageRequest(after_id=after))
            result.extend(page.items)
            if page.next_after_id is None:
                break
            after = page.next_after_id
    return tuple(sorted(result, key=lambda i: i.seq))


class CostCoordinator:
    def __init__(self, project, source_id):
        self.project, self.source_id = project, source_id
        self.estimates = {}
        self.confirmations = {}

    def remember(self, item, estimate):
        if type(estimate) is not int or estimate < 0:
            raise BackendError('cost:invalid_estimate', 'cost', retry='fatal')
        self.estimates[(item.item_id, max(1, item.attempts))] = estimate

    async def evaluate(self, item, estimate):
        self.remember(item, estimate)
        return await asyncio.to_thread(commands.evaluate_cost, self.project, self.source_id, estimated_anlas=estimate)

    async def confirm(self, expected_cycle_id):
        if expected_cycle_id in self.confirmations:
            return self.confirmations[expected_cycle_id]
        source = await asyncio.to_thread(commands.confirm_cost_and_resume, self.project, self.source_id,
                                         expected_cycle_id=expected_cycle_id)
        self.confirmations[expected_cycle_id] = source
        return source

    def account_status(self):
        items = all_items(self.project, self.source_id, ('pending', 'sent'))
        values = [self.estimates.get((i.item_id, max(1, i.attempts))) for i in items]
        known = [v for v in values if v is not None]
        return AccountStatus('local_estimate', None, None, None, sum(known) if known or not values else None,
                             len(values) - len(known), now())
