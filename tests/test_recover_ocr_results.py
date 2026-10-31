from home_budget_pipeline.receipts import ocr_results
from home_budget_pipeline.receipts.recover_results import eligible_deliveries, recover_deliveries, MISSING_SOURCE, MISSING_RUN
from test_ocr_results_collector import result_receipt


def deliveries(tmp_path):
    source = tmp_path / 'receipt.pdf'
    source.write_bytes(b'receipt')
    run, pass_event = ocr_results.result_messages_for_receipt(
        result_receipt(source), persistence_mode='cache_only',
        request_id='740023b1-a078-4914-b074-81bd7129bb75',
        trace_context={'traceparent': '00-' + 'a' * 32 + '-' + 'b' * 16 + '-01'},
    )
    return [(1, {'error_message': MISSING_RUN}, pass_event.to_bytes()),
            (2, {'error_message': MISSING_SOURCE}, run.to_bytes()),
            (3, {'error_message': 'unrelated'}, b'invalid')]


class Channel:
    def __init__(self): self.actions = []
    def basic_ack(self, **kwargs): self.actions.append(('ack', kwargs['delivery_tag']))
    def basic_nack(self, **kwargs):
        assert kwargs['requeue'] is True
        self.actions.append(('retain', kwargs['delivery_tag']))


def test_recovery_orders_parent_before_pass_and_commits_before_ack(tmp_path):
    channel = Channel()
    def process(event): channel.actions.append(('commit', event.event_type))
    report = recover_deliveries(channel, deliveries(tmp_path), process, apply=True)
    assert channel.actions == [('commit', ocr_results.RUN_COMPLETED), ('ack', 2),
                               ('commit', ocr_results.PASS_COMPLETED), ('ack', 1), ('retain', 3)]
    assert report['recovered'] == 2 and report['retained'] == 1


def test_preview_retains_every_message_and_never_invokes_persistence(tmp_path):
    channel = Channel()
    def process(event): raise AssertionError('Preview persisted a result')
    report = recover_deliveries(channel, deliveries(tmp_path), process, apply=False)
    assert report['eligible'] == 2 and report['recovered'] == 0
    assert channel.actions == [('retain', 1), ('retain', 2), ('retain', 3)]


def test_failed_commit_never_acknowledges_or_discards_original(tmp_path):
    channel = Channel()
    def process(event): raise RuntimeError('commit failed')
    report = recover_deliveries(channel, deliveries(tmp_path), process, apply=True)
    assert report['errors'] == {'RuntimeError': 2}
    assert report['recovered'] == 0
    assert all(action == 'retain' for action, _ in channel.actions)


def test_unmatched_pass_is_not_replayed_without_its_cache_only_parent(tmp_path):
    assert eligible_deliveries(deliveries(tmp_path)[:1]) == []
