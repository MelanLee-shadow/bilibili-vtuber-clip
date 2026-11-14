import json,hashlib
import gc
import weakref
import pytest
from src.autoslice.finding_closure_ports import RetainedObject,json_value
from src.autoslice.finding_closure import ClosureContractError


class _LifetimeText(str):
    """A supported JSON scalar whose lifetime can be observed without owning it."""


@pytest.mark.parametrize('api', ['from_value', 'json_value'])
@pytest.mark.parametrize('reject', [False, True], ids=['success', 'exception'])
def test_public_copy_releases_graph_without_cyclic_gc(api, reject):
    def call_and_drop_graph():
        marker = _LifetimeText('small lifetime probe')
        probe = weakref.ref(marker)
        # The completed child enters the copy memo before the invalid sibling
        # is visited. A failure at the root would not exercise memo cleanup.
        source = {'kept': [marker]}
        if reject:
            source['bad'] = object()
        operation = RetainedObject.from_value if api == 'from_value' else json_value
        if reject:
            try:
                operation(source)
            except ClosureContractError:
                pass
            else:
                pytest.fail('invalid sibling must be rejected')
        else:
            result = operation(source)
            if api == 'from_value':
                assert result.json_text == '{"kept":["small lifetime probe"]}'
            else:
                assert result == {'kept': ['small lifetime probe']}
                assert result is not source and result['kept'] is not source['kept']
        # Returning only a weakref drops source, marker, result, and any caught
        # exception traceback before the caller measures lifetime.
        return probe

    was_enabled = gc.isenabled()
    gc.disable()
    try:
        probe = call_and_drop_graph()
        assert probe() is None, 'completed JSON copy retained the dropped graph until cyclic GC'
    finally:
        # Also clean up the baseline's leaked closure on assertion failure.
        try:
            gc.collect()
        finally:
            if was_enabled:
                gc.enable()

@pytest.mark.parametrize('value',[{'中文':'😀\n\\\"','nested':(1,True,None,1.25)}, {'sorted':{'b':2,'a':1}}, {'empty':[],'n':-2}])
def test_factory_keeps_exact_canonical_bytes_and_sha(value):
 expected=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
 actual=RetainedObject.from_value(value)
 assert actual.json_text==expected and actual.sha256==hashlib.sha256(expected.encode()).hexdigest()
 assert RetainedObject(actual.json_text)==actual and actual.value()==json.loads(expected)

def test_shared_input_is_copied_once_but_detached_views_keep_ordinary_json_mutation_semantics():
 shared={'items':[{'text':'原始'}]};source={'first':shared,'second':shared}
 normalized=json_value(source)
 assert normalized['first'] is normalized['second'] and normalized['first'] is not shared
 actual=RetainedObject.from_value(source);source['first']['items'][0]['text']='输入后来改动'
 decoded=actual.value();decoded['first']['items'][0]['text']='消费者只改一支'
 assert decoded['second']['items'][0]['text']=='原始' and actual.value()['first']['items'][0]['text']=='原始'

@pytest.mark.parametrize('value',[{'bad':float('nan')},{'bad':float('inf')},{1:'bad'}, {'bad':object()}])
def test_factory_still_refuses_non_json_or_nonfinite(value):
 with pytest.raises(ClosureContractError):RetainedObject.from_value(value)

@pytest.mark.parametrize('raw',['{"b":2, "a":1}','{"x":NaN}','[]'])
def test_external_strings_still_use_full_canonical_validation(raw):
 with pytest.raises(ClosureContractError):RetainedObject(raw)

def test_subclass_factory_cannot_skip_its_own_constructor_contract():
 class Specialized(RetainedObject):
  def __post_init__(self):raise ValueError('subclass contract retained')
 with pytest.raises(ValueError,match='subclass contract'):Specialized.from_value({'x':1})


@pytest.mark.parametrize('value',[None,[],(),"not an object",123,1.25])
def test_factory_requires_object_root_even_on_trusted_factory(value):
 with pytest.raises(ClosureContractError,match='JSON_OBJECT_REQUIRED'):RetainedObject.from_value(value)

def test_cycle_is_rejected_before_canonical_encoding():
 value={};value['self']=value
 with pytest.raises(ClosureContractError):RetainedObject.from_value(value)

def test_large_unicode_hash_keeps_existing_unbroken_utf8_bytes():
 value={'text':('中😀\n\\'*20000)}
 actual=RetainedObject.from_value(value)
 assert len(actual.json_text)>65536
 assert actual.sha256==hashlib.sha256(actual.json_text.encode('utf-8')).hexdigest()


def test_unseen_mixed_shared_graph_has_one_source_visit_per_object_and_same_bytes():
 class ObservedDict(dict):
  visits=0
  def items(self):
   type(self).visits+=1
   return super().items()
 leaf=ObservedDict({'raw':'未参与真实样本的原答😀','windows':[1,2,3]})
 middle=ObservedDict({'tuple':(leaf,leaf),'list':[leaf,leaf]})
 value=ObservedDict({'a':middle,'b':middle,'c':[middle,middle]})
 expected=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
 ObservedDict.visits=0
 actual=RetainedObject.from_value(value)
 assert ObservedDict.visits==3
 assert actual.json_text==expected and actual.value()==json.loads(expected)
