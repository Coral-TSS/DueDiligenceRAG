"""Read-only eligibility must be proved from receipts, current sources and published parents."""
from uuid import uuid4
from test_assembly_ingestion import sample,step,drain


def inspect(x,project=None):
    from dd_agent.parent_coverage import ParentCoverage
    return ParentCoverage(x.db).inspect(project or x.p,x.d,x.index.version())


def test_closed_but_unpublished_is_not_eligible_then_current_publication_is(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=2)
    assert inspect(x)['eligible'] is False
    step(x)
    assert inspect(x)['eligible'] is False
    drain(x)
    result=inspect(x)
    assert result['eligible'] and result['covered_pages']==result['total_pages']==2
    assert result['expected_parents']>0 and result['unpublished_parents']==0
    assert inspect(x,uuid4()) is None
    x.db.save_page(x.d,'scan-v1',dict(page_number=2,status='succeeded',method='text',text='新来源'))
    assert inspect(x)['eligible'] is False


def test_blank_ready_pages_can_be_complete_without_vectors(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=2,blank=True)
    step(x);drain(x)
    result=inspect(x)
    assert result['eligible'] and result['covered_pages']==2
    assert result['expected_parents']==0
    assert x.index.models.calls==0


def test_wrong_version_and_missing_receipt_metadata_fail_closed(tmp_path,database_url):
    from dd_agent.parent_coverage import ParentCoverage
    x=sample(tmp_path,database_url,total=2)
    step(x);drain(x)
    assert inspect(x)['eligible']
    assert not ParentCoverage(x.db).inspect(x.p,x.d,'0'*64)['eligible']
    with x.db.connect() as conn:
        conn.execute("UPDATE dd_agent.assembly_batches SET result=result-'input_dependencies' WHERE document_id=%s",(x.d,))
    assert not inspect(x)['eligible']


def test_corrupt_or_reset_checkpoint_does_not_reuse_old_coverage(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=2)
    step(x);drain(x)
    assert inspect(x)['eligible']
    with x.db.connect() as conn:
        conn.execute("UPDATE dd_agent.assembly_checkpoints SET state=jsonb_set(state,'{checksum}','\"broken\"'::jsonb) WHERE document_id=%s",(x.d,))
    assert not inspect(x)['eligible']
    checkpoint=x.store.load(x.p,x.d)
    x.store.restart(x.p,x.d,expected_generation=checkpoint['generation'])
    assert not inspect(x)['eligible']


def test_multiwindow_tail_and_last_publication_must_finish_before_eligibility(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=13)
    step(x);drain(x)
    assert not inspect(x)['eligible']  # first ten pages do not prove whole-document coverage
    step(x)
    assert not inspect(x)['eligible']  # closed tail still has unpublished parents
    drain(x)
    result=inspect(x)
    assert result['eligible'] and result['covered_pages']==13
