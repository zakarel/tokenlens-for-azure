from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .ingest import message_text
from .models import AzureRecommendation, Estimate, Finding, RuleEvaluation, TraceRecord
from .tokens import count_chunks, count_messages, count_text, count_tools


RULES = [
    {"rule_id": "TL001", "title": "Repeated system prefix", "confidence": "high"},
    {"rule_id": "TL002", "title": "Conversation-history growth", "confidence": "high"},
    {"rule_id": "TL003", "title": "Unused tool definitions", "confidence": "high"},
    {"rule_id": "TL004", "title": "Retrieval redundancy", "confidence": "medium"},
    {"rule_id": "TL005", "title": "Retry amplification", "confidence": "high"},
    {"rule_id": "TL006", "title": "Output budget over-allocation", "confidence": "high"},
    {"rule_id": "TL007", "title": "Semantic-cache opportunity", "confidence": "medium"},
    {"rule_id": "TL008", "title": "Possible model over-sizing", "confidence": "low"},
]


@dataclass(frozen=True)
class RuleApplicability:
    """What a diagnostic needs before it is allowed to produce a verdict.

    Aggregate telemetry carries no prompts, no per-request identity, and no
    retry metadata. Running a request-level heuristic against it does not
    produce a weak finding — it produces a false one, because "no content"
    scores identically to "short, bounded content".

    ``required_any`` lists alternative evidence sets. A rule runs when *one*
    complete set is present, which is what lets the same rule work from raw
    content or from contentless fingerprints without ever running blind.
    """

    rule_id: str
    title: str
    request_telemetry: bool
    aggregate_telemetry: bool
    required_any: tuple[tuple[str, ...], ...]
    requirement: str

    @property
    def required_fields(self) -> tuple[str, ...]:
        return self.required_any[0] if self.required_any else ()


RULE_APPLICABILITY: tuple[RuleApplicability, ...] = (
    RuleApplicability(
        "TL001",
        "Repeated system prefix",
        True,
        False,
        (("system_prompt_fingerprint", "system_prompt_tokens"), ("messages",)),
        "a system-prompt fingerprint with its token count, or request messages",
    ),
    RuleApplicability(
        "TL002",
        "Conversation-history growth",
        True,
        False,
        (("messages",),),
        "per-request message content",
    ),
    RuleApplicability(
        "TL003",
        "Unused tool definitions",
        True,
        False,
        (("tools",),),
        "tool definitions with their call sites",
    ),
    RuleApplicability(
        "TL004",
        "Retrieval redundancy",
        True,
        False,
        (("retrieved_chunks",),),
        "retrieved context chunks",
    ),
    RuleApplicability(
        "TL005",
        "Retry amplification",
        True,
        True,
        (("retry_of",), ("retry_count",), ("messages",)),
        "explicit retry metadata, or request content to compare; an aggregate source needs a retry metric",
    ),
    RuleApplicability(
        "TL006",
        "Output budget over-allocation",
        True,
        False,
        (("max_output_tokens", "output_tokens"),),
        "a configured output limit and the observed output distribution",
    ),
    RuleApplicability(
        "TL007",
        "Semantic-cache opportunity",
        True,
        False,
        (("messages",),),
        "request content for equivalence comparison",
    ),
    RuleApplicability(
        "TL008",
        "Possible model over-sizing",
        True,
        False,
        (("model_identity", "messages"), ("model_identity", "task_shape_evidence")),
        "a known model plus request-level task-shape evidence",
    ),
)

APPLICABILITY_BY_RULE = {item.rule_id: item for item in RULE_APPLICABILITY}


def _finding(
    rule_id: str,
    severity: str,
    title: str,
    detail: str,
    evidence: dict[str, Any],
    estimate: Estimate,
    confidence: str,
    service: str,
    capability: str,
    action: str,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        title=title,
        detail=detail,
        evidence=evidence,
        estimated_savings=estimate,
        confidence=confidence,
        azure_recommendation=AzureRecommendation(
            service=service, capability=capability, action=action
        ),
    )


def _fingerprint_prefix(records: list[TraceRecord]) -> Finding | None:
    """Detect repeated system prefixes from HMAC fingerprints and token counts.

    Contentless telemetry carries a keyed fingerprint of the system prompt and
    its token count, which is enough to prove repetition without ever storing
    the prompt itself.
    """
    groups: dict[str, list[int]] = defaultdict(list)
    tokens: dict[str, int] = {}
    for index, record in enumerate(records):
        metadata = record.metadata or {}
        fingerprint = (metadata.get("fingerprints") or {}).get("system_prompt")
        prefix_tokens = (metadata.get("content_features") or {}).get("system_prompt_tokens")
        if not fingerprint or not prefix_tokens:
            continue
        groups[str(fingerprint)].append(index)
        tokens[str(fingerprint)] = int(prefix_tokens)
    if not groups:
        return None
    fingerprint, indexes = max(groups.items(), key=lambda item: len(item[1]))
    if len(indexes) < 2:
        return None
    prefix_tokens = tokens[fingerprint]
    repeated = prefix_tokens * (len(indexes) - 1)
    if repeated < 100:
        return None
    return _finding(
        "TL001",
        "high",
        "Repeated system prefix",
        f"{len(indexes):,} requests repeat a {prefix_tokens:,}-token stable prefix (matched by keyed fingerprint).",
        {
            "affected_requests": len(indexes),
            "prefix_tokens": prefix_tokens,
            "repeated_tokens": repeated,
            "evidence_source": "hmac_fingerprint",
        },
        Estimate(min_tokens=math.floor(repeated * 0.72), max_tokens=repeated, note="Stable prefix repetition; actual savings depend on cache eligibility."),
        "high",
        "Azure OpenAI",
        "Prompt caching",
        "Preserve a stable prompt prefix and measure cache-read coverage.",
    )


def _prefix(records: list[TraceRecord]) -> Finding | None:
    groups: dict[str, list[int]] = defaultdict(list)
    for index, record in enumerate(records):
        system = "\n".join(
            str(message.get("content", ""))
            for message in record.messages
            if message.get("role") == "system"
        ).strip()
        if system:
            groups[system].append(index)
    if not groups:
        return _fingerprint_prefix(records)
    prefix, indexes = max(groups.items(), key=lambda item: len(item[1]))
    if len(indexes) < 2:
        return None
    model = records[indexes[0]].model
    prefix_tokens = count_text(prefix, model)
    repeated = prefix_tokens * (len(indexes) - 1)
    if repeated < 100:
        return None
    return _finding(
        "TL001",
        "high",
        "Repeated system prefix",
        f"{len(indexes):,} requests repeat a {prefix_tokens:,}-token stable prefix.",
        {"affected_requests": len(indexes), "prefix_tokens": prefix_tokens, "repeated_tokens": repeated},
        Estimate(min_tokens=math.floor(repeated * 0.72), max_tokens=repeated, note="Stable prefix repetition; actual savings depend on cache eligibility."),
        "high",
        "Azure OpenAI",
        "Prompt caching",
        "Preserve a stable prompt prefix and measure cache-read coverage.",
    )


def _history(records: list[TraceRecord]) -> Finding | None:
    candidates = []
    total_history = 0
    total_input = 0
    for record in records:
        tokens = count_messages(record.messages, record.model)
        if len(record.messages) >= 5 and tokens > 0:
            candidates.append((record, tokens))
            total_history += tokens
            total_input += record.usage.input_tokens or tokens
    if len(candidates) < 2:
        return None
    average = round(total_history / len(candidates))
    estimated = round(total_history * 0.15)
    return _finding(
        "TL002",
        "medium",
        "Conversation-history growth",
        f"{len(candidates):,} requests carry five or more messages with a {average:,}-token average context.",
        {"affected_requests": len(candidates), "average_context_tokens": average, "total_context_tokens": total_history, "input_tokens_observed": total_input},
        Estimate(min_tokens=max(0, round(estimated * 0.6)), max_tokens=estimated, note="Opportunity assumes selective memory or summarization preserves task quality."),
        "high",
        "Azure OpenAI",
        "Context reduction",
        "Bound history, summarize durable state, and send only task-relevant turns.",
    )


def _tool_used(record: TraceRecord) -> set[str]:
    used = set()
    for message in record.messages:
        for call in message.get("tool_calls", []) or []:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            if function.get("name"):
                used.add(function["name"])
    return used


def _tools(records: list[TraceRecord]) -> Finding | None:
    named_tools = Counter()
    tool_tokens = 0
    used = Counter()
    for record in records:
        tool_tokens += count_tools(record.tools, record.model)
        for tool in record.tools:
            function = tool.get("function", {}) if isinstance(tool, dict) else {}
            if function.get("name"):
                named_tools[function["name"]] += 1
        used.update(_tool_used(record))
    unused = [name for name in named_tools if not used[name]]
    if not unused or tool_tokens < 100:
        return None
    unused_tokens = round(tool_tokens * len(unused) / max(1, len(named_tools)))
    return _finding(
        "TL003",
        "medium",
        "Unused tool definitions",
        f"{len(unused)} of {len(named_tools)} observed tools were never called in this trace set.",
        {"tools_observed": len(named_tools), "unused_tools": unused, "tool_schema_tokens": tool_tokens},
        Estimate(min_tokens=round(unused_tokens * 0.6), max_tokens=unused_tokens, note="Estimate assumes route-specific tool selection."),
        "high",
        "Azure API Management",
        "AI gateway policies",
        "Select tool definitions by route instead of sending the global tool catalog.",
    )


def _words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", value.lower()))


def _retrieval(records: list[TraceRecord]) -> Finding | None:
    with_chunks = [record for record in records if record.retrieved_chunks]
    if len(with_chunks) < 2:
        return None
    duplicate_tokens = 0
    comparisons = 0
    for record in with_chunks:
        chunks = [str(chunk) for chunk in record.retrieved_chunks]
        for left_index, left in enumerate(chunks):
            for right in chunks[left_index + 1 :]:
                comparisons += 1
                left_words, right_words = _words(left), _words(right)
                similarity = len(left_words & right_words) / max(1, len(left_words | right_words))
                if similarity >= 0.85:
                    duplicate_tokens += count_text(right, record.model)
    if duplicate_tokens < 100:
        return None
    return _finding(
        "TL004",
        "medium",
        "Retrieval redundancy",
        f"Detected {duplicate_tokens:,} potentially redundant retrieved tokens across {comparisons:,} chunk comparisons.",
        {"records_with_retrieval": len(with_chunks), "chunk_comparisons": comparisons, "redundant_tokens": duplicate_tokens},
        Estimate(min_tokens=round(duplicate_tokens * 0.5), max_tokens=duplicate_tokens, note="Lexical overlap is a screening signal; validate relevance before reducing top-K."),
        "medium",
        "Azure AI Search",
        "Hybrid retrieval and semantic reranking",
        "Improve chunking and rerank before increasing top-K.",
    )


def _retry(records: list[TraceRecord]) -> Finding | None:
    """Detect retry amplification from explicit retry metadata only.

    Content fingerprint inference is permitted only for request-level telemetry
    that carries nonempty, privacy-safe request text. An empty message list
    hashes identically for every record, which previously turned a silent
    aggregate window into "every request is a retry".
    """
    retry_indexes = [
        index
        for index, record in enumerate(records)
        if record.retry_of or int((record.metadata or {}).get("retry_count") or 0) > 0
    ]
    if not retry_indexes:
        fingerprints: dict[str, int] = defaultdict(int)
        signatures: list[str | None] = []
        for record in records:
            text = message_text(record.messages).strip()
            signature = hashlib.sha256(text.encode()).hexdigest() if text else None
            signatures.append(signature)
            if signature is not None:
                fingerprints[signature] += 1
        retry_indexes = [
            index
            for index, signature in enumerate(signatures)
            if signature is not None and fingerprints[signature] > 1
        ]
    if not retry_indexes:
        return None
    repeated = sum(records[index].usage.input_tokens or count_messages(records[index].messages, records[index].model) for index in retry_indexes)
    return _finding(
        "TL005",
        "high",
        "Retry amplification",
        f"{len(retry_indexes):,} retry requests resend an average of {round(repeated / len(retry_indexes)):,} input tokens.",
        {"retry_requests": len(retry_indexes), "repeated_input_tokens": repeated},
        Estimate(min_tokens=round(repeated * 0.6), max_tokens=repeated, note="Avoidable volume depends on retry cause and idempotency."),
        "high",
        "Azure API Management",
        "Retry and token policies",
        "Instrument retry causes and cap repeated context on recoverable failures.",
    )


def _p99(values: list[int]) -> int:
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, math.ceil(len(values) * 0.99) - 1)]


def _output_budget(records: list[TraceRecord]) -> Finding | None:
    pairs = [(record.max_output_tokens, record.usage.output_tokens) for record in records if record.max_output_tokens]
    if len(pairs) < 2:
        return None
    limit = _p99([int(pair[0]) for pair in pairs])
    output_p99 = _p99([int(pair[1]) for pair in pairs])
    if limit <= output_p99 * 2:
        return None
    gap = limit - output_p99
    return _finding(
        "TL006",
        "medium",
        "Output budget over-allocation",
        f"P99 output is {output_p99:,} tokens while the configured limit is {limit:,}.",
        {"requests_with_limit": len(pairs), "configured_limit_p99": limit, "output_p99": output_p99},
        Estimate(min_tokens=round(gap * len(pairs) * 0.1), max_tokens=gap * len(pairs), note="A lower limit constrains worst-case output; it does not reduce every response."),
        "high",
        "Azure OpenAI",
        "Output token controls",
        "Set route-specific output ceilings from measured distributions and quality tests.",
    )


def _cache(records: list[TraceRecord]) -> Finding | None:
    groups: dict[tuple[str, str, str], list[TraceRecord]] = defaultdict(list)
    for record in records:
        user_messages = [message for message in record.messages if message.get("role") == "user"]
        if not user_messages:
            continue
        prompt = re.sub(r"\s+", " ", str(user_messages[-1].get("content", ""))).strip().lower()
        workload = str(record.metadata.get("workload", "default"))
        tenant = str(record.metadata.get("tenant", "default"))
        if prompt:
            groups[(tenant, workload, prompt)].append(record)
    repeated = [group for group in groups.values() if len(group) > 1]
    if not repeated:
        return None
    reusable = sum(len(group) - 1 for group in repeated)
    return _finding(
        "TL007",
        "medium",
        "Semantic-cache opportunity",
        f"{reusable:,} requests belong to {len(repeated):,} repeated intent groups within workload and tenant boundaries.",
        {"repeated_requests": reusable, "intent_groups": len(repeated), "partitioning": "tenant + workload"},
        Estimate(min_tokens=reusable, max_tokens=reusable, unit="calls", note="Exact repeated prompts are a conservative cacheability signal; semantic matches require validation."),
        "medium",
        "Azure API Management",
        "Semantic caching with Azure Managed Redis",
        "Evaluate similarity thresholds, freshness, permissions, and tenant partitioning.",
    )


def _model_size(records: list[TraceRecord]) -> Finding | None:
    """Flag bounded work on a higher-capability model.

    Two guards keep this honest: the model must be identified, and the bounded
    verdict must come from observed request content or explicit task metadata.
    Absence of content is never evidence that a task was small.
    """
    bounded = 0
    eligible = 0
    evaluated = 0
    models = Counter()
    for record in records:
        if not _known_model(record):
            continue
        user_text = " ".join(
            str(message.get("content", ""))
            for message in record.messages
            if message.get("role") == "user"
        ).lower()
        task_shape = str((record.metadata or {}).get("task_type") or "").casefold()
        if not user_text.strip() and not task_shape:
            continue
        evaluated += 1
        models[record.model] += 1
        looks_bounded = any(
            term in user_text or term in task_shape
            for term in ("classify", "extract", "format as json", "yes or no", "categorize")
        )
        if looks_bounded or (len(user_text) < 120 and record.usage.output_tokens < 160):
            bounded += 1
        if "mini" not in record.model.lower() and "nano" not in record.model.lower():
            eligible += 1
    if not evaluated or bounded / evaluated < 0.2 or eligible / evaluated < 0.2:
        return None
    share = round(bounded / evaluated * 100)
    return _finding(
        "TL008",
        "low",
        "Possible model over-sizing",
        f"{share}% of traffic looks bounded or repetitive while using a higher-capability model.",
        {"bounded_requests": bounded, "requests_analyzed": evaluated, "models": dict(models)},
        Estimate(unit="none", note="No savings estimate; quality must be benchmarked."),
        "low",
        "Microsoft Foundry",
        "Model Router",
        "Benchmark Cost, Quality, and Balanced routing modes against a workload golden set.",
    )


def _known_model(record: TraceRecord) -> bool:
    """A model is known only when it is named; a deployment string is not a model."""
    model_name = (record.model_name or "").strip().casefold()
    return bool(model_name) and model_name not in {"unknown", "none"}


def _is_aggregate(record: TraceRecord) -> bool:
    return (record.metadata or {}).get("record_type") == "foundry_metric_bucket"


def _available_fields(records: list[TraceRecord]) -> set[str]:
    """Report the evidence actually present, never what a rule wishes existed."""
    available: set[str] = set()
    for record in records:
        metadata = record.metadata or {}
        features = metadata.get("content_features") or {}
        fingerprints = metadata.get("fingerprints") or {}
        if record.messages:
            available.update({"messages", "request_fingerprint", "task_shape_evidence"})
        if record.tools:
            available.add("tools")
        if record.retrieved_chunks:
            available.add("retrieved_chunks")
        if record.max_output_tokens:
            available.add("max_output_tokens")
        if record.usage.output_tokens:
            available.add("output_tokens")
        if record.retry_of:
            available.add("retry_of")
        if int(metadata.get("retry_count") or 0) > 0:
            available.add("retry_count")
        if metadata.get("task_type"):
            available.add("task_shape_evidence")
        if features.get("message_count") is not None:
            available.add("message_count")
        if features.get("tool_definition_tokens") is not None:
            available.add("tool_definition_tokens")
        if features.get("retrieval_tokens") is not None:
            available.add("retrieval_tokens")
        if features.get("max_output_tokens") is not None:
            available.add("max_output_tokens")
        if features.get("system_prompt_tokens") is not None:
            available.add("system_prompt_tokens")
        if fingerprints.get("system_prompt"):
            available.update({"system_prompt_fingerprint", "request_fingerprint"})
        if _known_model(record):
            available.add("model_identity")
    return available


def _source_kind(records: list[TraceRecord]) -> str:
    kinds = {"aggregate" if _is_aggregate(record) else "request" for record in records}
    if not kinds:
        return "none"
    if len(kinds) > 1:
        return "mixed"
    return kinds.pop()


def applicable_rules(records: list[TraceRecord]) -> tuple[list[str], list[RuleEvaluation]]:
    """Split the rule registry into runnable rules and not-evaluated statuses."""
    source = _source_kind(records)
    available = _available_fields(records)
    runnable: list[str] = []
    blocked: list[RuleEvaluation] = []
    for rule in RULE_APPLICABILITY:
        sources = [
            name
            for name, allowed in (("request", rule.request_telemetry), ("aggregate", rule.aggregate_telemetry))
            if allowed
        ]
        if source == "none":
            continue
        if source == "aggregate" and not rule.aggregate_telemetry:
            blocked.append(
                RuleEvaluation(
                    rule_id=rule.rule_id,
                    title=rule.title,
                    status="not_evaluated",
                    applicable_sources=sources,
                    required_fields=list(rule.required_fields),
                    missing_fields=list(rule.required_fields),
                    detail=(
                        f"{rule.title} needs {rule.requirement}. This analysis read aggregate metric buckets, "
                        "which carry no request-level evidence, so the rule was not evaluated. Absence of a "
                        "finding is not evidence that the workload is efficient."
                    ),
                )
            )
            continue
        missing = _missing_evidence(rule, available)
        if missing:
            blocked.append(
                RuleEvaluation(
                    rule_id=rule.rule_id,
                    title=rule.title,
                    status="not_evaluated",
                    applicable_sources=sources,
                    required_fields=list(rule.required_fields),
                    missing_fields=missing,
                    detail=(
                        f"{rule.title} needs {rule.requirement}, which this telemetry does not contain. "
                        "Absence of a finding is not evidence that the workload is efficient."
                    ),
                )
            )
            continue
        runnable.append(rule.rule_id)
    return runnable, blocked


def _missing_evidence(rule: RuleApplicability, available: set[str]) -> list[str]:
    """Return the closest unmet evidence set, or an empty list when the rule can run."""
    best: list[str] | None = None
    for group in rule.required_any:
        missing = [name for name in group if name not in available]
        if not missing:
            return []
        if best is None or len(missing) < len(best):
            best = missing
    return best or []


@dataclass
class RuleRun:
    """Findings plus the coverage statement that explains what was not run."""

    findings: list[Finding]
    evaluations: list[RuleEvaluation]
    source: str

    @property
    def not_evaluated(self) -> list[RuleEvaluation]:
        return [item for item in self.evaluations if item.status == "not_evaluated"]


_RULE_FUNCTIONS: dict[str, Callable[[list[TraceRecord]], Finding | None]] = {
    "TL001": _prefix,
    "TL002": _history,
    "TL003": _tools,
    "TL004": _retrieval,
    "TL005": _retry,
    "TL006": _output_budget,
    "TL007": _cache,
    "TL008": _model_size,
}


def evaluate_rules(records: list[TraceRecord]) -> RuleRun:
    """Run only the diagnostics this telemetry can actually support."""
    source = _source_kind(records)
    runnable, blocked = applicable_rules(records)
    findings: list[Finding] = []
    evaluations: list[RuleEvaluation] = list(blocked)
    for rule_id in runnable:
        rule = APPLICABILITY_BY_RULE[rule_id]
        finding = _RULE_FUNCTIONS[rule_id](records)
        if finding is not None:
            findings.append(finding)
            evaluations.append(
                RuleEvaluation(
                    rule_id=rule_id,
                    title=rule.title,
                    status="finding",
                    applicable_sources=["request"] + (["aggregate"] if rule.aggregate_telemetry else []),
                    required_fields=list(rule.required_fields),
                    detail=finding.detail,
                )
            )
        else:
            evaluations.append(
                RuleEvaluation(
                    rule_id=rule_id,
                    title=rule.title,
                    status="no_issue",
                    applicable_sources=["request"] + (["aggregate"] if rule.aggregate_telemetry else []),
                    required_fields=list(rule.required_fields),
                    detail=f"{rule.title} was evaluated against this telemetry and no issue was detected.",
                )
            )
    evaluations.sort(key=lambda item: item.rule_id)
    return RuleRun(findings=findings, evaluations=evaluations, source=source)


def run_rules(records: list[TraceRecord]) -> list[Finding]:
    """Backwards-compatible entry point returning evaluated findings only."""
    return evaluate_rules(records).findings
