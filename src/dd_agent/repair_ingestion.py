"""影子索引的自动修复适配器；默认关闭，生产调度仅为指定材料创建。"""
from .assembly_ingestion import AssemblyIngestion
from .assembly_repair import AssemblyRepair


class RepairingAssemblyIngestion(AssemblyIngestion):
    def __init__(self,index,stages,**kwargs):
        super().__init__(index,stages,**kwargs)
        self.repair=AssemblyRepair(stages)

    def _repair_progress(self,job,epoch,repair):
        result=super().status(job,expected_epoch=epoch)
        result['repair']=repair
        if repair['phase']=='done':
            # 接纳后仍须交接／向量完成和来源覆盖校验。来源又变时留给下一轮，
            # 不能把历史确认当作当前处理完成，也不提前结束父任务。
            if result['waiting']=='stale_sources':
                if self.repair.sources_current(job,expected_epoch=epoch):
                    result.update(terminal=True,failed=1,waiting='repair_no_progress')
                else:result.update(terminal=False,failed=0,waiting='repair_recheck')
        else:
            result.update(terminal=repair['terminal'],failed=repair['failed'],worked=repair['worked'],
                waiting=repair['error_code'] if repair['failed'] else 'repair_'+repair['phase'])
        self.progress=result
        return dict(result)

    def status(self,job_id,*,expected_epoch):
        repair=self.repair.status(job_id,expected_epoch=expected_epoch)
        if repair and repair['phase']!='done':return self._repair_progress(job_id,expected_epoch,repair)
        return super().status(job_id,expected_epoch=expected_epoch)

    def step(self,job_id,*,expected_epoch,vision_policy,reading_done=False,flush=False,checkpoint=None):
        repair=self.repair.status(job_id,expected_epoch=expected_epoch)
        if not repair or repair['phase']=='done':
            result=super().step(job_id,expected_epoch=expected_epoch,vision_policy=vision_policy,
                reading_done=reading_done,flush=flush,checkpoint=checkpoint)
            if not (result['closed'] and result['failed'] and result['waiting']=='stale_sources'):return result
        repair=self.repair.step(job_id,expected_epoch=expected_epoch,vision_policy=vision_policy,
            reading_done=reading_done,checkpoint=checkpoint)
        return self._repair_progress(job_id,expected_epoch,repair)
