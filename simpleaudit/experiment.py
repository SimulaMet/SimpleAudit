from typing import Any, Dict, List, Optional, Union
import asyncio

from tqdm import tqdm
from simpleaudit.results import AuditResults
from simpleaudit.model_auditor import ModelAuditor


class AuditExperiment:
    def __init__(
        self,
        models: List[Dict[str, Any]],
        judge_model: Optional[str] = None,
        judge_base_url: Optional[str] = None,
        judge_api_key: Optional[str] = None,
        judge_provider: Optional[str] = None,
    ):
        if not models or any("model" not in m for m in models):
            raise ValueError("Models must be dicts with a 'model' key.")
        self.models = models
        self.judge_model = judge_model
        self.judge_base_url = judge_base_url
        self.judge_api_key = judge_api_key
        self.judge_provider = judge_provider

    def _merge_common(self, model_info: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(model_info)
        if merged.get("judge_model") is None and self.judge_model is not None:
            merged["judge_model"] = self.judge_model
        if merged.get("judge_base_url") is None and self.judge_base_url is not None:
            merged["judge_base_url"] = self.judge_base_url
        if merged.get("judge_api_key") is None and self.judge_api_key is not None:
            merged["judge_api_key"] = self.judge_api_key
        if merged.get("judge_provider") is None and self.judge_provider is not None:
            merged["judge_provider"] = self.judge_provider
        return merged

    async def run(
        self,
        scenarios: Union[str, List[Dict]],
        max_turns: Optional[int] = None,
        language: str = "English",
        max_workers: int = 1,
    ) -> Dict[str, AuditResults]:
        results_by_model: Dict[str, AuditResults] = {}
        with tqdm(
            total=len(self.models),
            desc="Model Progress",
            position=2,
            leave=True,
        ) as pbar_models:
            for model_info in self.models:
                auditor = ModelAuditor(**self._merge_common(model_info))
                results_by_model[model_info["model"]] = await auditor.run_async(
                    scenarios,
                    max_turns=max_turns,
                    language=language,
                    max_workers=max_workers,
                )
                pbar_models.update(1)
        return results_by_model

    async def run_async(
        self,
        scenarios: Union[str, List[Dict]],
        max_turns: Optional[int] = None,
        language: str = "English",
        max_workers: int = 1,
    ) -> Dict[str, AuditResults]:
        return await self.run(
            scenarios,
            max_turns=max_turns,
            language=language,
            max_workers=max_workers,
        )

    def run_sync(
        self,
        scenarios: Union[str, List[Dict]],
        max_turns: Optional[int] = None,
        language: str = "English",
        max_workers: int = 1,
    ) -> Dict[str, AuditResults]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.run(
                    scenarios,
                    max_turns=max_turns,
                    language=language,
                    max_workers=max_workers,
                )
            )
        msg = "AuditExperiment.run_sync() cannot be called from an active event loop. Use await run()."
        raise RuntimeError(msg)