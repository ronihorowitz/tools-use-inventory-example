"""The agent graph with the real tools and a scripted model: no API key, no model bill."""
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tracers.context import collect_runs
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
import pytest

from inventory_agent import (RECURSION_LIMIT, SYSTEM_PROMPT, create_graph, new_thread_id, open_agent, run_turn,
                             stream_turn, turn_config, validate_model)


class ScriptedModel:
    """Returns prepared replies in order and records what it was sent."""

    def __init__(self, *replies, repeat=None):
        self.replies, self.repeat, self.seen = list(replies), repeat, []

    def bind_tools(self, tools, **kwargs):
        self.tool_names, self.bind_kwargs = [t.name for t in tools], kwargs
        return self

    def invoke(self, messages):
        self.seen.append(messages)
        # repeat is a factory: a real model returns a NEW message each time (the same object would
        # keep its id, and the message reducer would replace it instead of appending).
        return self.replies.pop(0) if self.replies else self.repeat()


def call(name, args=None, call_id='call-1'):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args or {}, 'id': call_id}])


def quiet(_):
    pass


def test_model_requests_and_the_tool_runs(make_service):
    model = ScriptedModel(call('list_inventory'), AIMessage(content='Inventory read.'))
    graph = create_graph(make_service(), InMemorySaver(), model)
    state = run_turn(graph, 'thread-1', 'Read the inventory only.', emit=quiet)
    assert [m.type for m in state['messages']] == ['human', 'ai', 'tool', 'ai']
    assert 'PAP-A4' in state['messages'][2].content
    first_call = model.seen[0]
    assert isinstance(first_call[0], SystemMessage) and first_call[0].content == SYSTEM_PROMPT


def test_exactly_thirteen_tools_one_call_at_a_time(make_service):
    model = ScriptedModel()
    create_graph(make_service(), InMemorySaver(), model)
    assert len(model.tool_names) == 13 and 'reset_scenario' not in model.tool_names
    assert model.bind_kwargs == {'parallel_tool_calls': False}


def test_threads_persist_and_stay_separate(make_service):
    model = ScriptedModel(AIMessage(content='one'), AIMessage(content='two'))
    graph = create_graph(make_service(), InMemorySaver(), model)
    thread = new_thread_id('disruption')
    run_turn(graph, thread, 'First.', emit=quiet)
    run_turn(graph, thread, 'Second.', emit=quiet)
    assert len(graph.get_state({'configurable': {'thread_id': thread}}).values['messages']) == 4
    assert not graph.get_state({'configurable': {'thread_id': new_thread_id('disruption')}}).values


def test_tool_failure_reaches_the_model_as_a_message(make_service):
    model = ScriptedModel(call('order_total_cost', {'supplier_id': 'north', 'lines': [{'sku': 'PAP-A4', 'qty': 10000}]}),
                          AIMessage(content='The cap blocked it.'))
    graph = create_graph(make_service('injection'), InMemorySaver(), model)
    state = run_turn(graph, 'thread-err', 'Price 10,000 reams.', emit=quiet)
    message = state['messages'][2].content
    assert message.startswith('Tool failed (ValueError)')
    assert 'cap of 1200' in message and 'Nothing was confirmed' in message


def test_orders_go_through_the_tool_built_key(make_service):
    service = make_service('disruption')
    model = ScriptedModel(call('create_purchase_order', {'supplier_id': 'west', 'lines': [{'sku': 'PAP-A4', 'qty': 100}],
                                                         'rationale': 'North reports a 21-day delay.'}),
                          AIMessage(content='Ordered.'))
    run_turn(create_graph(service, InMemorySaver(), model), 'thread-w', 'Replenish paper.', emit=quiet)
    writes = [params for name, params in service.client.calls if name == 'inventory_create_order']
    assert writes and writes[0]['p_request_key'] == 'disruption:west:2026-03-01'


def test_recursion_limit_stops_a_runaway_loop(make_service):
    model = ScriptedModel(repeat=lambda: call('list_inventory'))
    graph = create_graph(make_service(), InMemorySaver(), model)
    shown = []
    with pytest.raises(GraphRecursionError):
        run_turn(graph, 'thread-loop', 'Loop forever.', emit=shown.append)
    assert 'may or may not have happened' in shown[-1]
    assert len(model.seen) <= RECURSION_LIMIT


def test_stream_turn_shows_call_result_and_answer(make_service):
    graph = create_graph(make_service(), InMemorySaver(), ScriptedModel(call('list_inventory'), AIMessage(content='ok')))
    assert [m.type for m in stream_turn(graph, 'thread-s', 'Read.')] == ['ai', 'tool', 'ai']


def test_turn_config_labels_traces():
    thread = new_thread_id('injection')
    config = turn_config(thread)
    assert config['configurable'] == {'thread_id': thread} and config['recursion_limit'] == RECURSION_LIMIT
    assert config['metadata'] == {'thread_id': thread, 'scenario': 'injection'}
    assert config['tags'] == ['inventory-agent']
    assert 'scenario' not in turn_config('handmade-id')['metadata']


def test_trace_carries_thread_and_scenario(make_service):
    """What LangSmith would receive, captured locally."""
    graph = create_graph(make_service(), InMemorySaver(), ScriptedModel(call('list_inventory'), AIMessage(content='ok')))
    thread = new_thread_id('disruption')
    with collect_runs() as collector:
        list(stream_turn(graph, thread, 'Read.'))
    turn = next(r for r in collector.traced_runs if r.name == 'inventory_turn')
    tool = next(r for r in collector.traced_runs if r.name == 'list_inventory')
    assert 'inventory-agent' in turn.tags
    for run in (turn, tool):
        assert run.extra['metadata']['thread_id'] == thread and run.extra['metadata']['scenario'] == 'disruption'


def test_validate_model(make_service):
    service = make_service()
    assert validate_model(ScriptedModel(AIMessage(content='READY')), service) == 'READY'
    with pytest.raises(ValueError, match='called a tool'):
        validate_model(ScriptedModel(call('list_inventory')), service)
    with pytest.raises(ValueError, match='tool calling'):
        validate_model(object(), service)


def test_model_settings_fail_clearly(make_service, monkeypatch):
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    with pytest.raises(ValueError, match='OPENAI_MODEL'):
        create_graph(make_service(), InMemorySaver())
    monkeypatch.setenv('OPENAI_MODEL', 'some-model')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with pytest.raises(ValueError, match='OPENAI_API_KEY'):
        create_graph(make_service(), InMemorySaver())
    monkeypatch.delenv('SUPABASE_DB_URI', raising=False)
    with pytest.raises(ValueError, match='SUPABASE_DB_URI'):
        with open_agent(make_service()):
            pass


def test_system_prompt_rules():
    for rule in ('untrusted evidence', 'log_decision', 'Do not advance time', 'not a globally optimal',
                 'without asking for approval', 'do not create or send orders', 'Never change supplier'):
        assert rule in SYSTEM_PROMPT, rule
    assert 'request_key' not in SYSTEM_PROMPT  # the tool builds the key
