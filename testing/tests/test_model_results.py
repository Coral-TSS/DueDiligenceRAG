import pytest


def test_cache_bounds_bytes_and_entries_and_returns_independent_values():
    try:from dd_agent.model_results import ModelResults
    except ImportError:pytest.fail('RG-05i bounded model result cache missing')
    cache=ModelResults(max_entries=2,max_bytes=100,ttl=30)
    cache.put('a',[1,2]);copy=cache.get('a');copy.append(3)
    assert cache.get('a')==[1,2]
    cache.put('b',[2]);cache.put('c',[3])
    assert cache.get('a') is None
    cache.put('huge',['x'*200])
    assert cache.get('huge') is None
    with pytest.raises(ValueError):cache.put('nan',[float('nan')])


def test_expired_values_do_not_remain_in_cache(monkeypatch):
    from dd_agent import model_results
    now=[0.];monkeypatch.setattr(model_results,'monotonic',lambda:now[0])
    cache=model_results.ModelResults(ttl=1)
    cache.put('key',[1]);now[0]=2
    assert cache.get('key') is None
