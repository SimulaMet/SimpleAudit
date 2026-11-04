from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Union
import asyncio
import datetime
import functools
import hashlib
import inspect
import json
import numbers
import os
from urllib.parse import urlsplit, urlunsplit
import re
import threading
from collections import Counter

from tqdm.auto import tqdm
from simpleaudit._event_loop import run_sync
from simpleaudit.results import AuditResult, AuditResults, _atomic_json_dump
from simpleaudit.utils import UNGRADED, is_secret_name, redact_url_secrets
from simpleaudit.model_auditor import ModelAuditor, OnTurn
from simpleaudit.repeated_results import RepeatedExperimentResults


@dataclass
class ExperimentEvent:
    """Typed event yielded by :meth:`AuditExperiment.run_streamed`.

    Attributes:
        type: One of ``"rep_started"``, ``"rep_done"``, ``"model_done"``,
            ``"cancelled"``.
        model: Model label the event belongs to.
        rep_index: Zero-based rep index (``-1`` for non-rep events).
        total_reps: Total reps planned for this model.
        scenario_name: Set for scenario-level events.
        result: The :class:`AuditResults` for the rep (set on ``rep_done``).
            An adaptive rerun (``adaptive_reruns``) is reported with
            ``rep_index >= total_reps`` — to ``on_rep_done`` too — and its
            result covers only the fragile scenarios, not the full set.
        partial: Partial :class:`RepeatedExperimentResults` (set on ``model_done``).
    """

    type: str
    model: str
    rep_index: int = -1
    total_reps: int = 0
    scenario_name: Optional[str] = None
    result: Optional[AuditResults] = None
    partial: Optional["RepeatedExperimentResults"] = None




def _redact_secrets(value: Any, in_headers: bool = False) -> Any:
    """A copy of *value* with secret-looking keys, header values and URL credentials hidden."""
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if in_headers or is_secret_name(k)
            else _redact_secrets(v, in_headers="header" in str(k).lower())
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        if in_headers and len(value) == 2 and isinstance(value[0], str) and ":" not in value[0]:
            return [value[0], "[REDACTED]"]  # one ("Name", "value") header pair
        return [_redact_secrets(v, in_headers) for v in value]
    if in_headers and isinstance(value, str):
        # A header given as text ("Authorization: Bearer ...") or a bare value.
        name, sep, _ = value.partition(":")
        return f"{name}: [REDACTED]" if sep else "[REDACTED]"
    if isinstance(value, str) and "://" in value:
        return redact_url_secrets(value)
    return value


def _file_digest(uri: Any) -> Any:
    """A local attachment by its bytes, not its path; anything else as is.

    By path alone, regenerating an image or ASR clip in place reused runs of
    the old file, and moving the install (the built-in vision pack names
    absolute paths) quarantined valid runs.
    """
    if isinstance(uri, os.PathLike):
        uri = os.fspath(uri)
    if not isinstance(uri, str) or uri[:5].lower() == "data:":
        return uri
    path = uri
    if uri[:7].lower() == "file://":
        path = urlsplit(uri).path
    elif "://" in uri:
        return uri  # remote: not fetched for a fingerprint
    try:
        with open(os.path.expanduser(path), "rb") as handle:
            return "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()
    except (OSError, ValueError):
        return uri


def _scenario_for_digest(scenario: Any) -> Any:
    """*scenario* with local ``file_uri`` attachments replaced by content digests."""
    if not isinstance(scenario, dict) or not scenario.get("file_uri"):
        return scenario
    uris = scenario["file_uri"]
    digested = (
        [_file_digest(u) for u in uris] if isinstance(uris, (list, tuple)) else _file_digest(uris)
    )
    return {**scenario, "file_uri": digested}


def _is_int(value: Any) -> bool:
    # numbers.Integral: numpy integers (np.int64(3)) are accepted, bools not.
    return isinstance(value, numbers.Integral) and not isinstance(value, bool)


def _quarantine(path: Path) -> None:
    """Rename *path* to ``<name>.stale``, never over an earlier quarantine.

    A fixed name would let a second config switch (a typo'd judge, then
    back) replace the paid runs set aside the first time with ERROR runs.
    Later quarantines get ``.stale-1``, ``.stale-2``, ...
    """
    target = path.with_name(path.name + ".stale")
    n = 0
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}.stale-{n}")
    path.replace(target)


def _rerunnable_error(result: AuditResult) -> bool:
    """An ERROR whose scenario should be run again: not a judge failure.

    ``judgment.error_phase == "judge"`` marks a complete transcript the judge
    could not grade; it stays ERROR (see ``reframing.rejudge`` to re-grade
    it). Errors without a phase (older files, whole-scenario failures) are
    treated as re-runnable, as before.
    """
    if result.severity != "ERROR":
        return False
    judgment = result.judgment if isinstance(result.judgment, dict) else {}
    # Only a recorded failure (judgment["error"]) is re-run. An ERROR the
    # judge's *reply* produced — unparseable, ungradable, self-contradicting —
    # carries no "error" and is a judge outcome on a complete transcript, the
    # same as a raised judge call.
    # A bare ERROR with no judgment at all (hand-built, or from an old file)
    # is treated as a failed run, as before.
    if not judgment:
        return True
    return bool(judgment.get("error")) and judgment.get("error_phase") != "judge"


def _legacy_fingerprint_default(value: Any) -> str:
    """The pre-0.5.2 JSON fallback, kept so legacy caches still match."""
    if callable(value):
        return f"{getattr(value, '__module__', '')}.{getattr(value, '__qualname__', type(value).__name__)}"
    return str(value)


def _code_digest(func: Any, _depth: int = 0) -> str:
    """A short digest of what a callable *does*: its source (stable across
    Python versions), of every function it wraps (``functools.wraps``), and
    of the callables it closes over (``_chain``'s parts). Bytecode is the
    fallback when the source is unavailable (a lambda typed in a REPL)."""
    if _depth > 5:
        return ""
    parts = []
    target = func
    while target is not None:
        try:
            parts.append(inspect.getsource(target))
        except Exception:  # OSError, TypeError, TokenError/SyntaxError on an edited file
            code = getattr(target, "__code__", None)
            parts.append(code.co_code.hex() if code is not None else "")
        for cell in getattr(target, "__closure__", None) or ():
            try:
                contents = cell.cell_contents
            except ValueError:
                continue
            if callable(contents):
                parts.append(_code_digest(contents, _depth + 1))
        target = getattr(target, "__wrapped__", None)
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:12]


# Per thread: experiments may fingerprint the same shared hook concurrently,
# and one thread's in-progress digest must not look like a cycle to another.
_VISITING_LOCAL = threading.local()


def _state_digest(state: Any) -> str:
    """A short digest of captured values (instance state, partial or closure
    arguments). Only the digest is shown: the values may be credentials, and
    the fingerprint source is written to config.json. A value that refers
    back to itself digests as a fixed marker, so the result does not depend
    on how deep the stack was."""
    visiting = getattr(_VISITING_LOCAL, "ids", None)
    if visiting is None:
        visiting = _VISITING_LOCAL.ids = set()
    key = id(state)
    if key in visiting:
        return "cycle"
    visiting.add(key)
    try:
        text = json.dumps(state, sort_keys=True, default=_fingerprint_default)
    except (TypeError, ValueError, RecursionError):  # unserialisable
        text = repr(type(state))
    finally:
        visiting.discard(key)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _without_credentials(value: Any, in_headers: bool = False) -> Any:
    """*value* with only actual credentials removed, for the fingerprint digest.

    Narrower than ``_redact_secrets`` (the copy shown in config.json): a
    non-secret header value (``api-version: 2025``) or a URL's user name
    changes what a run does and must change the fingerprint; a password,
    token or secret-named value must not enter the digest stored beside it.
    The "Name: value" header forms are only read under a header key, so a
    prompt that starts "Secret: ..." is digested in full.
    """
    if isinstance(value, dict):
        return {
            k: "[credential]" if is_secret_name(k)
            else _without_credentials(v, in_headers or "header" in str(k).lower())
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        if (in_headers and len(value) == 2 and isinstance(value[0], str)
                and ":" not in value[0] and is_secret_name(value[0])):
            return [value[0], "[credential]"]  # a ("Authorization", "...") pair
        return [_without_credentials(v, in_headers) for v in value]
    if isinstance(value, str):
        if in_headers:
            name, sep, _rest = value.partition(":")
            if sep and is_secret_name(name.strip()):
                return f"{name}: [credential]"  # "Authorization: Bearer ..."
        if re.match(r"^\w+://", value):
            # A URL: keep the user name (it selects an account), drop only
            # the password; secret query values go too.
            try:
                parts = urlsplit(value)
                user, password = parts.username, parts.password
            except ValueError:  # malformed (e.g. "http://[::1/v1")
                return redact_url_secrets(value)
            host = parts.netloc.rsplit("@", 1)[-1]
            if password is not None:
                netloc = f"{user}:[credential]@{host}"
            elif user is not None:
                netloc = f"{user}@{host}"
            else:
                netloc = host
            bare = urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
            redacted = urlsplit(redact_url_secrets(bare))
            return urlunsplit((parts.scheme, netloc, redacted.path, redacted.query, redacted.fragment))
        if "://" in value:
            return redact_url_secrets(value)  # other schemes / URLs inside text
    return value


def _fingerprint_default(value: Any) -> str:
    """JSON fallback for fingerprints: callables (a composed judge's
    post-processor) by name and by a digest of their code — so an edited or
    upgraded grading hook does not reuse runs graded by the old logic — since
    their repr carries a memory address that changes between processes."""
    # Data a Python-written pack may hold. The type-only fallback below would
    # digest every date as "<datetime.date>", so an edited validity window
    # (what a groundedness judge grades against) would reuse stale runs.
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        # By canonical JSON, not repr: a nested frozenset's repr follows hash
        # randomisation, so the order (and fingerprint) would differ per process.
        return sorted(
            value, key=lambda v: json.dumps(v, sort_keys=True, default=_fingerprint_default)
        )
    if isinstance(value, functools.partial):
        # The wrapped function and its bound arguments, not just "partial".
        # The arguments appear only as a digest: config.json is written from
        # this string, and they may hold a credential.
        return f"partial({_fingerprint_default(value.func)}#" + _state_digest(
            [list(value.args), dict(value.keywords)]
        ) + ")"
    if callable(value) and not isinstance(value, type) and not hasattr(value, "__code__") \
            and hasattr(type(value), "__call__") and hasattr(type(value).__call__, "__code__"):
        # A callable instance: its class's __call__ code (instance state that
        # is not JSON is not visible).
        cls = type(value)
        # Its settings too (Post("pass") vs Post("critical")), as a digest.
        state = _state_digest(getattr(value, "__dict__", {}))
        return f"<{cls.__module__}.{cls.__qualname__}#{_code_digest(cls.__call__)}#{state}>"
    if inspect.ismethod(value):
        # A bound method: its function and the settings of the object it is
        # bound to (Floor("pass").apply vs Floor("critical").apply).
        owner = value.__self__
        cls = owner if isinstance(owner, type) else type(owner)
        state = _state_digest(getattr(owner, "__dict__", {})) if not isinstance(owner, type) else ""
        return f"<{cls.__module__}.{cls.__qualname__}.{value.__func__.__name__}#{_code_digest(value.__func__)}#{state}>"
    if callable(value) and not isinstance(value, type):
        name = f"{getattr(value, '__module__', '')}.{getattr(value, '__qualname__', type(value).__name__)}"
        # Closures with the same name differ by their captured values
        # (binary_postprocess(True) vs (False)); include the ones JSON can show.
        cells = []
        for cell in getattr(value, "__closure__", None) or ():
            try:
                contents = cell.cell_contents
                json.dumps(contents)
            except (ValueError, TypeError):
                continue
            cells.append(contents)
        if hasattr(value, "__code__") or hasattr(value, "__wrapped__"):
            name = f"{name}#{_code_digest(value)}"
        return f"{name}#{_state_digest(cells)}" if cells else name
    # Other objects (an injected judge_client) by type: str() carries a memory
    # address, which would change the fingerprint on every resume.
    return f"<{type(value).__module__}.{type(value).__qualname__}>"


class AuditExperiment:
    def __init__(
        self,
        models: List[Dict[str, Any]],
        judge_model: Optional[str] = None,
        judge_base_url: Optional[str] = None,
        judge_api_key: Optional[str] = None,
        judge_provider: Optional[str] = None,
        auditor_model: Optional[str] = None,
        auditor_provider: Optional[str] = None,
        auditor_api_key: Optional[str] = None,
        auditor_base_url: Optional[str] = None,
        judge: Optional[Union[str, Dict[str, Any]]] = None,
        probe_prompt: Optional[str] = None,
        judge_prompt: Optional[str] = None,
        judge_response_schema: Optional[Dict[str, Any]] = None,
        judge_kwargs: Optional[Dict[str, Any]] = None,
        auditor_kwargs: Optional[Dict[str, Any]] = None,
        judge_params: Optional[Dict[str, Any]] = None,
        auditor_params: Optional[Dict[str, Any]] = None,
        json_format: bool = True,
        verbose: bool = False,
        show_progress: bool = True,
        n_repetitions: int = 1,
        adaptive_reruns: Optional[Dict[str, Any]] = None,
        save_dir: Optional[str] = None,
        on_model_done: Optional[Callable[[str, "RepeatedExperimentResults"], None]] = None,
        on_rep_done: Optional[Callable[..., Any]] = None,
        cancel_event: Optional[asyncio.Event] = None,
        max_retries_per_rep: int = 2,
        rep_is_done: Optional[Callable[..., Any]] = None,
        max_error_reps: int = 3,
    ):
        if not models or any("model" not in m for m in models):
            raise ValueError("Models must be dicts with a 'model' key.")
        # Integers, checked up front: a float fails only in range() after
        # the paid reps have run.
        if not _is_int(n_repetitions) or n_repetitions < 1:
            raise ValueError("n_repetitions must be an integer >= 1")
        if adaptive_reruns is not None:
            if not isinstance(adaptive_reruns, dict):
                raise ValueError("adaptive_reruns must be a dict")
            if "agreement_target" not in adaptive_reruns:
                raise ValueError("adaptive_reruns requires 'agreement_target'")
            target = adaptive_reruns["agreement_target"]
            if not 0 < target <= 1:
                raise ValueError("adaptive_reruns['agreement_target'] must be in (0, 1]")
            max_extra = adaptive_reruns.get("max_extra", 5)
            if not _is_int(max_extra) or max_extra < 0:
                raise ValueError("adaptive_reruns['max_extra'] must be an integer >= 0")

        labels = [m.get("label") or m["model"] for m in models]
        if len(labels) != len(set(labels)):
            raise ValueError(
                "Duplicate model labels detected. Add a 'label' key to distinguish "
                "models sharing the same 'model' value."
            )
        # The run cache on disk is keyed by the SANITIZED label, so labels
        # that only differ in '/', ':', or ' ' (e.g. 'org/model' vs
        # 'org:model') would share a cache directory and silently swap each
        # other's results on resume. Reject them up front.
        # casefold: the default macOS and Windows filesystems ignore case, so
        # 'Baseline' and 'baseline' are one directory there.
        safe_labels = [self._sanitize_label(label).casefold() for label in labels]
        if len(safe_labels) != len(set(safe_labels)):
            raise ValueError(
                "Model labels collide after filesystem sanitization ('/', ':' "
                "and ' ' all map to '_'). Add distinct 'label' keys so each "
                "model gets its own run cache directory."
            )

        self.models = models
        self.judge_model = judge_model
        self.judge_base_url = judge_base_url
        self.judge_api_key = judge_api_key
        self.judge_provider = judge_provider
        self.auditor_model = auditor_model
        self.auditor_provider = auditor_provider
        self.auditor_api_key = auditor_api_key
        self.auditor_base_url = auditor_base_url
        self.judge = judge
        self.probe_prompt = probe_prompt
        self.judge_prompt = judge_prompt
        self.judge_response_schema = judge_response_schema
        # Judge / auditor client kwargs and per-request params shared by every
        # model; a model entry's own values win (see _merge_common).
        self.judge_kwargs = judge_kwargs
        self.auditor_kwargs = auditor_kwargs
        self.judge_params = judge_params
        self.auditor_params = auditor_params
        self.json_format = json_format
        self.verbose = verbose
        self.show_progress = show_progress
        # Stored as plain int: a numpy integer passes validation but cannot be
        # written to the saved JSON after every rep has run.
        self.n_repetitions = int(n_repetitions)
        self.adaptive_reruns = (
            {**adaptive_reruns, "max_extra": int(adaptive_reruns["max_extra"])}
            if adaptive_reruns is not None and "max_extra" in adaptive_reruns
            else adaptive_reruns
        )
        self.save_dir = Path(save_dir) if save_dir else None
        self.on_model_done = on_model_done
        self.on_rep_done = on_rep_done
        self.cancel_event = cancel_event
        if not _is_int(max_retries_per_rep) or max_retries_per_rep < 0:
            raise ValueError("max_retries_per_rep must be an integer >= 0")
        if not _is_int(max_error_reps) or max_error_reps < 0:
            raise ValueError("max_error_reps must be an integer >= 0")
        self.max_retries_per_rep = int(max_retries_per_rep)
        self.rep_is_done = rep_is_done
        self.max_error_reps = int(max_error_reps)

    def _make_label(self, model_info: Dict[str, Any]) -> str:
        return model_info.get("label") or model_info["model"]

    def _merge_common(self, model_info: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(model_info)
        merged.pop("label", None)  # label is experiment-level only; ModelAuditor doesn't accept it
        if merged.get("judge_model") is None and self.judge_model is not None:
            merged["judge_model"] = self.judge_model
        if merged.get("judge_base_url") is None and self.judge_base_url is not None:
            merged["judge_base_url"] = self.judge_base_url
        if merged.get("judge_api_key") is None and self.judge_api_key is not None:
            merged["judge_api_key"] = self.judge_api_key
        if merged.get("judge_provider") is None and self.judge_provider is not None:
            merged["judge_provider"] = self.judge_provider
        for key in ("auditor_model", "auditor_provider", "auditor_api_key", "auditor_base_url"):
            if merged.get(key) is None and getattr(self, key) is not None:
                merged[key] = getattr(self, key)
        if merged.get("judge") is None and self.judge is not None:
            merged["judge"] = self.judge
        if merged.get("probe_prompt") is None and self.probe_prompt is not None:
            merged["probe_prompt"] = self.probe_prompt
        if merged.get("judge_prompt") is None and self.judge_prompt is not None:
            merged["judge_prompt"] = self.judge_prompt
        # provider/judge_provider are documented optional: leave explicit
        # values alone, but guarantee the keys exist so ModelAuditor's
        # required parameters are satisfied (None resolves to its default
        # provider there).
        merged.setdefault("provider", None)
        merged.setdefault("judge_provider", None)
        if merged.get("json_format") is None:
            merged["json_format"] = self.json_format
        if merged.get("judge_response_schema") is None and self.judge_response_schema is not None:
            merged["judge_response_schema"] = self.judge_response_schema
        for key in ("judge_kwargs", "auditor_kwargs", "judge_params", "auditor_params"):
            if merged.get(key) is None and getattr(self, key) is not None:
                merged[key] = getattr(self, key)
        if merged.get("verbose") is None:
            merged["verbose"] = self.verbose
        if merged.get("show_progress") is None:
            merged["show_progress"] = self.show_progress
        return merged

    @staticmethod
    def _sanitize_label(label: str) -> str:
        """Replace characters that are unsafe in directory names."""
        safe = label.replace("/", "_").replace("\\", "_").replace(":", "_").replace(" ", "_")
        if safe in ("", ".", ".."):
            # Would resolve to save_dir itself or its parent.
            raise ValueError(f"Model label {label!r} cannot be used as a directory name.")
        return safe

    def _run_path(self, label: str, index: int) -> Path:
        return self.save_dir / self._sanitize_label(label) / f"run_{index}.json"

    def _config_path(self, label: str) -> Path:
        return self.save_dir / self._sanitize_label(label) / "config.json"

    @staticmethod
    def _config_fingerprint(
        merged: Dict[str, Any],
        scenarios: Union[str, List[Dict]],
        max_turns: Optional[int],
        language: str,
    ) -> Dict[str, Any]:
        """Describe the configuration a cached run was produced under.

        API keys are excluded (never written to disk), and so are display
        settings (``verbose``, ``show_progress``), which do not change a run.
        An inline scenario list is identified by each scenario's name plus a
        digest of its content, so editing a scenario's text under the same
        name invalidates its cached runs; a pack name is digested with its
        expanded content, so an upgraded pack is not served from old runs.

        ``legacy_fingerprint`` is the pre-0.5.2 formula (names only, display
        settings included); a cache written by an older version is still
        accepted when it matches that, so upgrading does not re-run it.
        """
        config = {
            k: v for k, v in merged.items()
            # Settings that cannot change a verdict: display, transport retries
            # and the progress callback. Changing them must not re-run paid reps.
            if "api_key" not in k and k not in (
                "verbose", "show_progress", "max_retries", "retry_backoff", "on_turn",
            )
        }
        if isinstance(scenarios, str):
            # The pack's content too: a library upgrade that edits a pack must
            # not reuse runs of its old text.
            scenario_id: Any = [scenarios, hashlib.sha256(
                json.dumps([_scenario_for_digest(s) for s in ModelAuditor.get_scenarios(scenarios)],
                           sort_keys=True, default=_fingerprint_default).encode("utf-8")
            ).hexdigest()[:16]]
        else:
            scenario_id = [
                [s.get("name"), hashlib.sha256(
                    json.dumps(_scenario_for_digest(s), sort_keys=True,
                               default=_fingerprint_default).encode("utf-8")
                ).hexdigest()[:16]]
                for s in scenarios
            ]
        judge_spec = merged.get("judge")
        if judge_spec is None:
            # The built-in default judge (the common setup): its rubric, probe
            # prompt, schema and parser, unless the caller overrode them.
            from simpleaudit import model_auditor as _ma
            from simpleaudit.utils import parse_json_response

            config["judge_content"] = hashlib.sha256(
                json.dumps(
                    [
                        None if merged.get("judge_prompt") else
                        [_ma.DEFAULT_JUDGE_CRITERIA, _ma.DEFAULT_JUDGE_SEVERITY_LEVELS],
                        None if merged.get("probe_prompt") else _ma.DEFAULT_PROBE_PROMPT,
                        _ma.DEFAULT_JUDGE_RESPONSE_SCHEMA,
                        merged.get("judge_postprocess") or parse_json_response,
                    ],
                    sort_keys=True, default=_fingerprint_default,
                ).encode("utf-8")
            ).hexdigest()[:16]
        elif isinstance(judge_spec, (str, dict)):
            # The judge's content too, like a pack's: an upgrade that edits a
            # named judge's prompt must not reuse runs graded by the old one.
            # The post-processor actually applied is resolved here (format
            # hooks and consistency checks are not stored in the config), so
            # a change to grading logic is caught as well.
            from simpleaudit.judges import get_judge
            from simpleaudit.judges.compose import inherited_postprocess

            judge_config = get_judge(judge_spec)
            resolved = (
                merged.get("judge_postprocess")
                or inherited_postprocess(judge_config, merged.get("judge_prompt") is not None)
            )
            config["judge_content"] = hashlib.sha256(
                json.dumps(
                    [judge_config, resolved, judge_config.get("grade")],
                    sort_keys=True, default=_fingerprint_default,
                ).encode("utf-8")
            ).hexdigest()[:16]
        source = {
            "version": 2,
            "config": config,
            "scenarios": scenario_id,
            # The turn count the run will use: None means the auditor's
            # max_turns (or its default), so None and the same explicit value
            # must not fingerprint differently and quarantine valid runs.
            "max_turns": max_turns if max_turns is not None else merged.get(
                "max_turns",
                inspect.signature(ModelAuditor.__init__).parameters["max_turns"].default,
            ),
            "language": language,
        }
        legacy_source = {
            "config": {k: v for k, v in merged.items() if "api_key" not in k},
            "scenarios": scenarios if isinstance(scenarios, str) else [s.get("name") for s in scenarios],
            "max_turns": max_turns,
            "language": language,
        }

        def _digest(value: Any) -> str:
            return hashlib.sha256(
                json.dumps(value, sort_keys=True, default=_fingerprint_default).encode("utf-8")
            ).hexdigest()

        # The digest covers every value; the copy written to config.json has
        # credentials redacted (headers in *_kwargs, secret keys, URL
        # userinfo), since save_dir is often shared or committed.
        # Digest the redacted source: config.json stores it beside the
        # digest, and an unredacted digest would let anyone who can read the
        # file test guesses for a password or Basic credential offline.
        return {
            "fingerprint": _digest(_without_credentials(source)),
            "legacy_fingerprint": hashlib.sha256(
                json.dumps(legacy_source, sort_keys=True, default=_legacy_fingerprint_default).encode("utf-8")
            ).hexdigest(),
            "source": _redact_secrets(source),
        }

    def _check_run_config(
        self, label: str, fingerprint: Dict[str, Any], quarantine_combined: bool = True
    ) -> bool:
        """Return True when cached runs for this label are safe to reuse.

        A config.json that disagrees with the current configuration means the
        cached runs were produced under different settings (another judge,
        prompt, scenario pack, ...) — mixing them into the aggregates would
        silently corrupt the experiment, so the cache is rejected. A missing
        config.json (caches from older versions) is accepted. Either way the
        current fingerprint is written for the next resume.

        Rejected run files are renamed to ``*.stale`` before the new
        fingerprint is written. Leaving them in place would let a later
        resume — which now sees a matching fingerprint — load runs produced
        under the old configuration as if they were current.
        """
        path = self._config_path(label)
        reusable = True
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    stored = json.load(f)
                if not isinstance(stored, dict):
                    raise ValueError("config.json is not an object")
                matches = stored.get("fingerprint") == fingerprint["fingerprint"] or (
                    # A cache from before the versioned fingerprint.
                    "version" not in (stored.get("source") or {})
                    and stored.get("fingerprint") == fingerprint.get("legacy_fingerprint")
                )
                if not matches:
                    tqdm.write(
                        f"  Warning: cached runs for {label!r} were produced under a "
                        "different configuration — ignoring them and re-running "
                        f"(delete {path.parent} to silence this)."
                    )
                    reusable = False
            except (ValueError, KeyError, TypeError):
                pass  # unreadable marker: treat like a legacy cache
        if not reusable:
            self._quarantine_runs(path.parent)
            # The combined results file was written under the old config too.
            combined = self.save_dir / "experiment_results.json"
            if quarantine_combined and combined.exists():
                _quarantine(combined)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                # The legacy digest covers unredacted credentials (the 0.5.1
                # formula); it is only compared, never stored.
                stored = {k: v for k, v in fingerprint.items() if k != "legacy_fingerprint"}
                json.dump(stored, f, indent=2, ensure_ascii=False, default=_fingerprint_default)
            tmp_path.replace(path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return reusable

    @staticmethod
    def _quarantine_runs(directory: Path) -> None:
        """Rename cached run files in ``directory`` to ``*.stale``."""
        for pattern in ("run_*.json", "adaptive_*.json"):
            for run_file in directory.glob(pattern):
                _quarantine(run_file)

    def _adaptive_settings(self) -> Dict[str, Any]:
        return {"adaptive_reruns": self.adaptive_reruns, "n_repetitions": self.n_repetitions}

    def _write_adaptive_settings(self, label: str) -> None:
        path = self.save_dir / self._sanitize_label(label) / "adaptive-settings.json"
        _atomic_json_dump(self._adaptive_settings(), str(path))

    def _load_cached_adaptive(self, label: str, max_extra: int) -> List[AuditResults]:
        """Adaptive reruns saved by an earlier invocation, in order, up to the first gap.

        Only when they were chosen under the same adaptive settings and
        repetition count (``adaptive-settings.json``): another agreement target
        or rep set would have chosen other reruns.
        """
        directory = self.save_dir / self._sanitize_label(label)
        try:
            with open(directory / "adaptive-settings.json", "r", encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, ValueError):
            stored = None
        if stored != self._adaptive_settings():
            for old in directory.glob("adaptive_*.json"):
                _quarantine(old)
            return []
        loaded: List[AuditResults] = []
        for k in range(max_extra):
            path = self._adaptive_path(label, k)
            if not path.exists():
                break
            try:
                loaded.append(AuditResults.load(str(path)))
            except (ValueError, KeyError, TypeError):
                break
        return loaded

    def _adaptive_path(self, label: str, index: int) -> Path:
        return self.save_dir / self._sanitize_label(label) / f"adaptive_{index}.json"

    @staticmethod
    def _scenario_cache_label(label: str, scenario: Dict) -> str:
        """Cache label for :meth:`run_scenario_reps`: one directory per scenario.

        Keyed on the full scenario content, so separate scenarios for the same
        model don't overwrite each other's run files, and an edited scenario
        never reuses runs of its previous text.
        """
        digest = hashlib.sha256(
            json.dumps(_scenario_for_digest(scenario), sort_keys=True,
                       default=_fingerprint_default).encode("utf-8")
        ).hexdigest()[:12]
        return f"{label}@{digest}"

    def _load_cached_runs(
        self,
        label: str,
        expected_names: Optional[set] = None,
        partial: Optional[Dict[int, AuditResults]] = None,
    ) -> Dict[int, AuditResults]:
        """Return {index: AuditResults} for every reusable run_N.json on disk.

        A cached rep with an ERROR result is not reused as it is: when
        *partial* is given it is collected there, and the caller re-runs only
        its errored scenarios (as ``_run_single_rep`` does live), keeping the
        verdicts it already has. Without *partial* the rep is re-run whole.
        If more than ``max_error_reps`` reps errored on EVERY scenario, the
        entire model's cache is invalidated (the configuration is likely
        fundamentally broken).
        """
        cached: Dict[int, AuditResults] = {}
        error_count = 0
        errored_reps: Dict[int, AuditResults] = {}
        for i in range(self.n_repetitions):
            path = self._run_path(label, i)
            if path.exists():
                try:
                    results = AuditResults.load(str(path))
                except (ValueError, KeyError, TypeError) as exc:
                    tqdm.write(
                        f"  Warning: ignoring unreadable cached run {path} "
                        f"({type(exc).__name__}: {exc}) — this run will be re-executed"
                    )
                    continue
                if expected_names is not None and {r.scenario_name for r in results} < expected_names:
                    # A run over a strict subset of the scenarios (an adaptive
                    # rerun saved as run_N.json by older versions) is not a
                    # full repetition.
                    tqdm.write(
                        f"  Warning: ignoring cached run {path}: it does not cover the "
                        "scenario set — this run will be re-executed"
                    )
                    continue
                if any(_rerunnable_error(r) for r in results):
                    # One scenario that always fails (a context-length 400)
                    # must not make every resume re-sample the good verdicts.
                    if all(_rerunnable_error(r) for r in results):
                        error_count += 1
                    errored_reps[i] = results
                    continue
                cached[i] = results
        # If too many reps are broken, invalidate everything.
        if error_count > self.max_error_reps:
            tqdm.write(
                f"  Warning: {error_count}/{self.n_repetitions} cached runs for "
                f"{label!r} contain errors (threshold {self.max_error_reps}) — "
                "invalidating full cache."
            )
            return {}
        if partial is not None:
            partial.update(errored_reps)
        return cached

    # ------------------------------------------------------------------
    # Internal helpers for the fine-grained execution path
    # ------------------------------------------------------------------

    async def _call_rep_is_done(self, label: str, rep_index: int) -> bool:
        """Check the external idempotency hook. Returns True if the slot is done."""
        if self.rep_is_done is None:
            return False
        result = self.rep_is_done(label, rep_index)
        if inspect.iscoroutine(result):
            result = await result
        return bool(result)

    async def _call_on_rep_done(self, label: str, rep_index: int, result: AuditResults) -> None:
        """Invoke the on_rep_done callback (sync or async)."""
        if self.on_rep_done is None:
            return
        cb = self.on_rep_done(label, rep_index, self.n_repetitions, result)
        if inspect.iscoroutine(cb):
            await cb

    async def _run_single_rep(
        self,
        merged: Dict[str, Any],
        scenarios: Union[str, List[Dict]],
        max_turns: Optional[int],
        language: str,
        max_workers: int,
        on_turn: Optional[OnTurn] = None,
        audit_run_id: Optional[str] = None,
        trace_correlation: Optional[Any] = None,
        evidence_resolver: Optional[Callable] = None,
        existing: Optional[AuditResults] = None,
    ) -> AuditResults:
        """Execute one rep, re-running only the scenarios that ended in ERROR.

        ERROR results are infrastructure failures with no verdict. Up to
        ``max_retries_per_rep`` extra attempts re-run just those scenarios
        (not the whole rep, so clean verdicts are never re-rolled) and splice
        the new results in place. Whatever still errors after that stays
        ERROR and is excluded from scoring downstream.

        *existing* is a cached rep with errors (resume): its clean verdicts
        are kept and only its errored scenarios are run.
        """
        attempts = 1 + self.max_retries_per_rep
        scenario_list: List[Dict[str, Any]] = (
            ModelAuditor.get_scenarios(scenarios) if isinstance(scenarios, str) else list(scenarios)
        )
        names = [s.get("name") for s in scenario_list]
        # Splicing by name needs unique names; fall back to whole-rep retries.
        by_name = len(set(names)) == len(names)
        result: Optional[AuditResults] = None
        to_run: List[Dict[str, Any]] = scenario_list
        if existing is not None and by_name and {r.scenario_name for r in existing} >= set(names):
            errored_before = {r.scenario_name for r in existing if _rerunnable_error(r)}
            result = existing
            to_run = [s for s in scenario_list if s.get("name") in errored_before]
            if not to_run:
                return existing
        for attempt in range(attempts):
            auditor = ModelAuditor(**merged)
            # trace_correlation may be a zero-arg callable (resolved per rep so
            # the caller can swap in a fresh correlation at each rep boundary).
            corr = trace_correlation() if callable(trace_correlation) else trace_correlation
            partial = await auditor.run_async(
                to_run,
                max_turns=max_turns,
                language=language,
                max_workers=max_workers,
                on_turn=on_turn,
                audit_run_id=audit_run_id,
                trace_correlation=corr,
                evidence_resolver=evidence_resolver,
            )
            if result is None or not by_name:
                result = partial
            else:
                fresh = {r.scenario_name: r for r in partial}
                merged_results = [fresh.get(r.scenario_name, r) for r in result]
                result = AuditResults(merged_results, meta=partial.meta)
                result.timestamp = partial.timestamp
            # Only failures to *produce* the transcript are re-run. A judge
            # failure leaves a complete transcript; re-running it would
            # resample the target, and when the judge fails because of the
            # transcript (a content filter on harmful output, a context
            # overflow) the retry keeps rolling until the target produces
            # something gradable — usually a refusal — and reports a pass.
            if isinstance(scenarios, str):
                # The pack was expanded to a list above; keep its name in the
                # run meta as a direct ModelAuditor run would.
                result.meta["scenario_pack"] = scenarios
            errored = [r.scenario_name for r in result if _rerunnable_error(r)]
            if not errored:
                break
            if attempt < attempts - 1:
                tqdm.write(
                    f"  {len(errored)} scenario(s) returned ERROR "
                    f"(attempt {attempt + 1}/{attempts}) — re-running "
                    + ("just those" if by_name else "the rep")
                )
                if by_name:
                    to_run = [s for s in scenario_list if s.get("name") in set(errored)]
        return result  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Public: single-scenario, multi-rep execution
    # ------------------------------------------------------------------

    async def run_scenario_reps(
        self,
        model_index: int,
        scenario: Dict[str, Any],
        max_turns: Optional[int] = None,
        language: str = "English",
        on_turn: Optional[OnTurn] = None,
        audit_run_id: Optional[str] = None,
        trace_correlation: Optional[Any] = None,
        evidence_resolver: Optional[Callable] = None,
    ) -> List[AuditResult]:
        """Run a single scenario N times for one model.

        Builds a fresh :class:`ModelAuditor` per rep (independent
        conversations). Respects ``n_repetitions``, ``cancel_event``,
        ``on_rep_done``, ``max_retries_per_rep``, ``rep_is_done``, and the
        disk cache (per-rep files under ``save_dir``).

        ``rep_is_done(label, i)`` and ``on_rep_done(label, i, n, result)``
        are keyed by model label and rep index only, not by scenario: they
        suit one ``AuditExperiment`` per scenario (as SimpleAudit Studio
        uses it). Sharing one experiment and one done-set across scenarios
        would mark later scenarios' reps as done; key your store by
        scenario as well, or use separate experiments.

        Args:
            model_index: Index into ``self.models``.
            scenario: A single scenario dict.
            max_turns: Override for max conversation turns.
            language: Language for probe generation.
            on_turn: Optional callback fired at each phase boundary with
                ``(turn_index, max_turns, role)`` where role is "auditor",
                "target", or "judge". Called synchronously from within the
                asyncio event loop. The full contract, including what fires on
                a failure and on a retried rep, is documented on
                :data:`simpleaudit.model_auditor.OnTurn`.
            audit_run_id: Optional run id propagated to each rep's
                ``run_async`` for trace correlation.
            trace_correlation: Optional :class:`TraceCorrelation` shared
                across reps; each rep records its ``turn_id -> trace_id``
                links so the caller can fetch per-rep trace evidence.
            evidence_resolver: Optional callable invoked after target
                execution and before judge in each rep. May return a span
                list or a dict with ``selected_spans`` + ``agentic_evaluation``
                for agentic-aware judging.

        Returns:
            List of :class:`AuditResult`, one per completed rep. May be
            shorter than ``n_repetitions`` if cancellation occurred.
        """
        model_info = self.models[model_index]
        label = self._make_label(model_info)
        merged = self._merge_common(model_info)
        results: List[AuditResult] = []

        cache_label = self._scenario_cache_label(label, scenario)
        cache_reusable = False
        if self.save_dir:
            fingerprint = self._config_fingerprint(merged, [scenario], max_turns, language)
            # run_scenario_reps never writes experiment_results.json; one in
            # this save_dir belongs to a run_streamed experiment.
            cache_reusable = self._check_run_config(
                cache_label, fingerprint, quarantine_combined=False
            )

        for i in range(self.n_repetitions):
            # Cancellation check
            if self.cancel_event is not None and self.cancel_event.is_set():
                break

            # External idempotency: skip if caller says this slot is done
            if await self._call_rep_is_done(label, i):
                continue

            # Disk cache: only reuse if the file was produced for THIS scenario
            # under the same configuration (fingerprint checked above).
            if self.save_dir and cache_reusable:
                run_path = self._run_path(cache_label, i)
                if run_path.exists():
                    try:
                        cached = AuditResults.load(str(run_path))
                        # Validate: must contain exactly this scenario, no errors
                        if (
                            len(cached) == 1
                            and cached[0].scenario_name == scenario["name"]
                            and not any(_rerunnable_error(r) for r in cached)
                        ):
                            results.extend(cached)
                            continue
                    except (ValueError, KeyError, TypeError):
                        pass  # fall through and re-run

            # Execute with auto-retry
            rep_result = await self._run_single_rep(
                merged, [scenario], max_turns, language, max_workers=1,
                on_turn=on_turn,
                audit_run_id=audit_run_id,
                trace_correlation=trace_correlation,
                evidence_resolver=evidence_resolver,
            )

            # Persist
            if self.save_dir:
                run_path = self._run_path(cache_label, i)
                run_path.parent.mkdir(parents=True, exist_ok=True)
                rep_result.save(str(run_path))

            results.extend(rep_result)

            # Callback
            await self._call_on_rep_done(label, i, rep_result)

        return results

    # ------------------------------------------------------------------
    # Public: streaming async generator
    # ------------------------------------------------------------------

    async def run_streamed(
        self,
        scenarios: Union[str, List[Dict]],
        max_turns: Optional[int] = None,
        language: str = "English",
        max_workers: int = 1,
    ) -> AsyncIterator[ExperimentEvent]:
        """Execute the experiment, yielding typed events as progress is made.

        This is the core execution loop. :meth:`run_async` is a thin wrapper
        that consumes this generator and returns the final result.

        Yields:
            ExperimentEvent with type in {"rep_started", "rep_done",
            "model_done", "cancelled"}.
        """
        judge_info = {
            k: v for k, v in {
                "judge_model": self.judge_model,
                "judge_base_url": redact_url_secrets(self.judge_base_url) if self.judge_base_url else None,
                "judge_provider": self.judge_provider,
                "judge_name": self.judge if isinstance(self.judge, str) else None,
            }.items() if v is not None
        } or None

        runs_by_model: Dict[str, List[AuditResults]] = {}
        adaptive_by_model: Dict[str, List[AuditResults]] = {}
        cancelled = False

        with tqdm(
            total=len(self.models),
            desc="Models",
            position=3,
            leave=True,
            disable=not self.show_progress,
        ) as pbar_models:
            for model_info in self.models:
                label = self._make_label(model_info)
                merged = self._merge_common(model_info)

                # Load cached runs
                cached: Dict[int, AuditResults] = {}
                # Cached reps with errors: only their errored scenarios re-run.
                partial_reps: Dict[int, AuditResults] = {}
                if self.save_dir:
                    fingerprint = self._config_fingerprint(
                        merged, scenarios, max_turns, language
                    )
                    if self._check_run_config(label, fingerprint):
                        expected = {
                            s.get("name") for s in (
                                ModelAuditor.get_scenarios(scenarios)
                                if isinstance(scenarios, str) else scenarios
                            )
                        }
                        cached = self._load_cached_runs(label, expected, partial_reps)
                if cached:
                    tqdm.write(f"  Resuming {label}: {len(cached)}/{self.n_repetitions} runs found on disk")
                # Adaptive reruns are tied to the full runs they followed; they
                # are resumed only when every full run came from the cache.
                all_cached = len(cached) == self.n_repetitions
                stale_adaptive_cleared = False

                with tqdm(
                    total=self.n_repetitions,
                    desc=f"{label} — repetitions",
                    position=2,
                    leave=False,
                    disable=(not self.show_progress or self.n_repetitions == 1),
                ) as pbar_reps:
                    pbar_reps.update(len(cached))

                    runs_ordered: Dict[int, AuditResults] = dict(cached)
                    for i in range(self.n_repetitions):
                        # Cancellation
                        if self.cancel_event is not None and self.cancel_event.is_set():
                            yield ExperimentEvent(
                                type="cancelled", model=label,
                                rep_index=i, total_reps=self.n_repetitions,
                            )
                            cancelled = True
                            break

                        # External idempotency
                        if await self._call_rep_is_done(label, i):
                            pbar_reps.update(1)
                            continue

                        if i in cached:
                            continue

                        # A full rep is about to be (re-)run: adaptive reruns
                        # saved earlier were chosen against the old rep set,
                        # whatever this invocation does later (it may be
                        # cancelled, or run without adaptive_reruns).
                        if self.save_dir and not stale_adaptive_cleared:
                            stale_adaptive_cleared = True
                            directory = self.save_dir / self._sanitize_label(label)
                            for old in directory.glob("adaptive_*.json"):
                                _quarantine(old)

                        # Yield rep_started
                        yield ExperimentEvent(
                            type="rep_started", model=label,
                            rep_index=i, total_reps=self.n_repetitions,
                        )

                        # Execute with auto-retry
                        result = await self._run_single_rep(
                            merged, scenarios, max_turns, language, max_workers,
                            existing=partial_reps.get(i),
                        )

                        # Persist
                        if self.save_dir:
                            run_path = self._run_path(label, i)
                            run_path.parent.mkdir(parents=True, exist_ok=True)
                            result.save(str(run_path))

                        runs_ordered[i] = result
                        pbar_reps.update(1)

                        # Callback + yield rep_done (full AuditResults)
                        await self._call_on_rep_done(label, i, result)
                        yield ExperimentEvent(
                            type="rep_done", model=label,
                            rep_index=i, total_reps=self.n_repetitions,
                            result=result,
                        )

                runs_list = [runs_ordered[i] for i in range(self.n_repetitions) if i in runs_ordered]
                if (
                    self.adaptive_reruns and not cancelled
                    and self.cancel_event is not None and self.cancel_event.is_set()
                ):
                    # Cancelled during the last full rep: no adaptive phase.
                    yield ExperimentEvent(
                        type="cancelled", model=label,
                        rep_index=self.n_repetitions, total_reps=self.n_repetitions,
                    )
                    cancelled = True

                # Adaptive reruns (routed through the same event/callback path).
                # Each rerun covers only the fragile scenarios, so it is kept
                # apart from the full repetitions: it adds verdicts to the
                # per-scenario statistics but not to the run count or the
                # whole-run scores, and it is saved as adaptive_N.json so a
                # later resume never mistakes it for a full run_N.json.
                adaptive_list: List[AuditResults] = []
                if self.adaptive_reruns and not cancelled and runs_list:
                    target = self.adaptive_reruns["agreement_target"]
                    max_extra = self.adaptive_reruns.get("max_extra", 5)
                    scenario_pool = (
                        ModelAuditor.get_scenarios(scenarios)
                        if isinstance(scenarios, str) else list(scenarios)
                    )
                    if self.save_dir and all_cached and not stale_adaptive_cleared:
                        adaptive_list = self._load_cached_adaptive(label, max_extra)
                        if adaptive_list:
                            tqdm.write(
                                f"  Resuming {label}: {len(adaptive_list)} adaptive rerun(s) found on disk"
                            )
                    for extra in range(len(adaptive_list), max_extra):
                        if self.cancel_event is not None and self.cancel_event.is_set():
                            yield ExperimentEvent(
                                type="cancelled", model=label,
                                rep_index=self.n_repetitions + extra,
                                total_reps=self.n_repetitions,
                            )
                            cancelled = True
                            break
                        scenario_severities: Dict[str, List[str]] = {}
                        for run in runs_list + adaptive_list:
                            for r in run:
                                # No verdict, no vote: ERROR/UNGRADED are left
                                # out, as in the stability report, so a
                                # scenario that keeps failing does not keep
                                # triggering reruns.
                                if r.severity in ("ERROR", UNGRADED):
                                    continue
                                scenario_severities.setdefault(r.scenario_name, []).append(r.severity)
                        below_target = [
                            name for name, sevs in scenario_severities.items()
                            if Counter(sevs).most_common(1)[0][1] / len(sevs) < target
                        ]
                        if not below_target:
                            break
                        tqdm.write(
                            f"  Adaptive rerun {extra + 1}/{max_extra} for {label}: "
                            f"{len(below_target)} scenario(s) below agreement target {target}"
                        )
                        fragile_scenarios = [
                            s for s in scenario_pool if s.get("name") in below_target
                        ]
                        if not fragile_scenarios:
                            break
                        # After every full repetition slot, so an adaptive rerun
                        # never reports the index of a real (perhaps skipped)
                        # repetition to on_rep_done / rep_is_done consumers.
                        new_index = self.n_repetitions + extra
                        yield ExperimentEvent(
                            type="rep_started", model=label,
                            rep_index=new_index, total_reps=self.n_repetitions,
                        )
                        result = await self._run_single_rep(
                            merged, fragile_scenarios, max_turns, language, max_workers
                        )
                        if self.save_dir:
                            run_path = self._adaptive_path(label, extra)
                            run_path.parent.mkdir(parents=True, exist_ok=True)
                            result.save(str(run_path))
                            self._write_adaptive_settings(label)
                        adaptive_list.append(result)
                        await self._call_on_rep_done(label, new_index, result)
                        yield ExperimentEvent(
                            type="rep_done", model=label,
                            rep_index=new_index, total_reps=self.n_repetitions,
                            result=result,
                        )

                # Skip models with zero runs (e.g. pre-cancelled)
                if not runs_list:
                    continue
                runs_by_model[label] = runs_list
                if adaptive_list:
                    adaptive_by_model[label] = adaptive_list

                # model_done event (only for models that have runs)
                partial = RepeatedExperimentResults(
                    {label: runs_by_model[label]}, judge=judge_info, cancelled=cancelled,
                    adaptive_runs={label: adaptive_list},
                    n_repetitions=self.n_repetitions,
                )
                if self.on_model_done:
                    # Sync or async, like on_rep_done / rep_is_done.
                    maybe = self.on_model_done(label, partial)
                    if inspect.isawaitable(maybe):
                        await maybe
                yield ExperimentEvent(
                    type="model_done", model=label,
                    total_reps=self.n_repetitions, partial=partial,
                )
                pbar_models.update(1)

        # Final save
        if self.save_dir:
            self.save_dir.mkdir(parents=True, exist_ok=True)
            final = RepeatedExperimentResults(
                runs_by_model, judge=judge_info, cancelled=cancelled,
                adaptive_runs=adaptive_by_model,
                n_repetitions=self.n_repetitions,
            )
            final.save(str(self.save_dir / "experiment_results.json"))

    # ------------------------------------------------------------------
    # Public: batch execution (backward-compatible)
    # ------------------------------------------------------------------

    async def run_async(
        self,
        scenarios: Union[str, List[Dict]],
        max_turns: Optional[int] = None,
        language: str = "English",
        max_workers: int = 1,
    ) -> RepeatedExperimentResults:
        """Run the full experiment. Thin wrapper over :meth:`run_streamed`."""
        judge_info = {
            k: v for k, v in {
                "judge_model": self.judge_model,
                "judge_base_url": redact_url_secrets(self.judge_base_url) if self.judge_base_url else None,
                "judge_provider": self.judge_provider,
                "judge_name": self.judge if isinstance(self.judge, str) else None,
            }.items() if v is not None
        } or None

        runs_by_model: Dict[str, List[AuditResults]] = {}
        adaptive_by_model: Dict[str, List[AuditResults]] = {}
        cancelled = False

        async for event in self.run_streamed(
            scenarios, max_turns=max_turns, language=language, max_workers=max_workers
        ):
            if event.type == "model_done" and event.partial is not None:
                for label, runs in event.partial.all_runs().items():
                    runs_by_model[label] = runs
                    adaptive_by_model[label] = event.partial.adaptive_runs(label)
            if event.type == "cancelled":
                cancelled = True

        experiment_results = RepeatedExperimentResults(
            runs_by_model, judge=judge_info, cancelled=cancelled,
            adaptive_runs=adaptive_by_model,
            n_repetitions=self.n_repetitions,
        )
        return experiment_results

    def run(
        self,
        scenarios: Union[str, List[Dict]],
        max_turns: Optional[int] = None,
        language: str = "English",
        max_workers: int = 1,
    ) -> RepeatedExperimentResults:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return run_sync(
                self.run_async(
                    scenarios,
                    max_turns=max_turns,
                    language=language,
                    max_workers=max_workers,
                )
            )
        msg = "AuditExperiment.run() cannot be called from an active event loop. Use await <object>.run_async()."
        raise RuntimeError(msg)
