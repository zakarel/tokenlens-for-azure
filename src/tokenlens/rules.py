from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

from .ingest import message_text
from .models import AzureRecommendation, Estimate, Finding, TraceRecord
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
    evaluated: bool = True,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        title=title,
        detail=detail,
        evidence=evidence,
        estimated_savings=estimate,
        confidence=confidence,
        evaluated=evaluated,
        azure_recommendation=AzureRecommendation(
            service=service, capability=capability, action=action
        ),
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
        return None
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
        return _finding(
            "TL004",
            "info",
            "Retrieval redundancy",
            "Not evaluated: retrieved chunk text is missing from most records.",
            {"records_with_retrieval": len(with_chunks), "records_analyzed": len(records)},
            Estimate(unit="none", note="Add retrieved_chunks to evaluate overlap."),
            "not_evaluated",
            "Azure AI Search",
            "Hybrid retrieval and semantic reranking",
            "Log retrieved chunks so overlap and reranking quality can be measured.",
            evaluated=False,
        )
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
    retry_indexes = [index for index, record in enumerate(records) if record.retry_of]
    if not retry_indexes:
        fingerprints: dict[str, int] = defaultdict(int)
        for record in records:
            fingerprint = hashlib.sha256(message_text(record.messages).encode()).hexdigest()
            fingerprints[fingerprint] += 1
        retry_indexes = []
        for index, record in enumerate(records):
            fingerprint = hashlib.sha256(message_text(record.messages).encode()).hexdigest()
            if fingerprints[fingerprint] > 1:
                retry_indexes.append(index)
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
    bounded = 0
    eligible = 0
    models = Counter()
    for record in records:
        models[record.model] += 1
        user_text = " ".join(
            str(message.get("content", ""))
            for message in record.messages
            if message.get("role") == "user"
        ).lower()
        looks_bounded = any(
            term in user_text
            for term in ("classify", "extract", "format as json", "yes or no", "categorize")
        )
        if looks_bounded or (len(user_text) < 120 and record.usage.output_tokens < 160):
            bounded += 1
        if "mini" not in record.model.lower() and "nano" not in record.model.lower():
            eligible += 1
    if not records or bounded / len(records) < 0.2 or eligible / len(records) < 0.2:
        return None
    share = round(bounded / len(records) * 100)
    return _finding(
        "TL008",
        "low",
        "Possible model over-sizing",
        f"{share}% of traffic looks bounded or repetitive while using a higher-capability model.",
        {"bounded_requests": bounded, "requests_analyzed": len(records), "models": dict(models)},
        Estimate(unit="none", note="No savings estimate; quality must be benchmarked."),
        "low",
        "Microsoft Foundry",
        "Model Router",
        "Benchmark Cost, Quality, and Balanced routing modes against a workload golden set.",
    )


def run_rules(records: list[TraceRecord]) -> list[Finding]:
    findings: list[Finding] = []
    for rule in (_prefix, _history, _tools, _retrieval, _retry, _output_budget, _cache, _model_size):
        finding = rule(records)
        if finding:
            findings.append(finding)
    return findings
