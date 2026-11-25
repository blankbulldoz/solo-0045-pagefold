from .successions import archive_with_succession


def archive(store, plan_id, revision, sources=None, drill_fail=False):
    return archive_with_succession(store, plan_id, revision, sources, drill_fail)
