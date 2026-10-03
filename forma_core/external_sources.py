from __future__ import annotations

from forma_core.config import config
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from pydantic import BaseModel, Field

from forma_core.agents.firecrawl_mcp import FirecrawlMCPResearchClient


DEFAULT_EXTERNAL_SOURCE_PROVIDER = "firecrawl"
SUPPORTED_EXTERNAL_SOURCE_PROVIDERS = {"auto", "none", "disabled", "off", "tavily", "firecrawl"}


def _env_bool(name: str, default: bool = False, settings: Optional[Mapping[str, str]] = None) -> bool:
    value = settings.get(name) if settings is not None else config.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int, settings: Optional[Mapping[str, str]] = None) -> int:
    raw = settings.get(name) if settings is not None else config.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, min(maximum, int(raw.strip())))
    except ValueError:
        return default


def _env_float(name: str, default: float, minimum: float, maximum: float, settings: Optional[Mapping[str, str]] = None) -> float:
    raw = settings.get(name) if settings is not None else config.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, min(maximum, float(raw.strip())))
    except ValueError:
        return default


def _first_env(names: Iterable[str], settings: Optional[Mapping[str, str]] = None) -> Optional[str]:
    for name in names:
        value = settings.get(name) if settings is not None else config.get(name)
        if value and value.strip():
            return value.strip()
    return None


class ExternalSourceRecord(BaseModel):
    title: str = ""
    url: str = ""
    content: str = ""
    source_type: str = "web"
    provider: str = ""
    score: Optional[float] = None
    published_at: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def as_prompt_block(self, index: int, *, max_chars: int) -> str:
        text = (self.content or "").strip() or self.title or self.url
        block = f"[{index}] {self.title or 'Untitled source'}\nProvider: {self.provider or 'unknown'}\nURL: {self.url or 'unknown'}\n{text}"
        if len(block) > max_chars:
            return block[: max_chars - 3] + "..." if max_chars >= 3 else block[:max_chars]
        return block

    def metadata_preview(self, *, max_chars: int = 360) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "provider": self.provider,
            "source_type": self.source_type,
            "score": self.score,
            "published_at": self.published_at,
            "content_preview": self.content[:max_chars],
        }


class ExternalSourceLibrary(BaseModel):
    provider: str
    configured: bool
    searches_attempted: int = 0
    sources: list[ExternalSourceRecord] = Field(default_factory=list)
    answer: Optional[str] = None
    error: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def hits(self) -> list[ExternalSourceRecord]:
        return self.sources

    def as_prompt_context(self, max_chars: int = 14_000) -> str:
        """Render normalized external sources as bounded LLM context.

        Args:
            max_chars: Maximum total context length.

        Returns:
            Prompt-ready external research context.
        """
        if self.error:
            raise RuntimeError(
                f"{self.provider} external research failed: {self.error}"
            )

        if not self.sources:
            return (
                f"{self.provider} completed {self.searches_attempted} searches "
                "but returned no usable external sources."
            )

        blocks: list[str] = []
        remaining = max(0, max_chars)

        if self.answer and remaining > 0:
            answer_block = (
                f"{self.provider} answer summary:\n"
                f"{self.answer.strip()}"
            )
            answer_block = answer_block[: min(3_000, remaining)]
            blocks.append(answer_block)
            remaining -= len(answer_block)

        sources_remaining = len(self.sources)

        for index, source in enumerate(self.sources, start=1):
            if remaining <= 0 or sources_remaining <= 0:
                break

            separator_length = 2 if blocks else 0
            available = remaining - separator_length
            if available <= 0:
                break

            fair_share = max(500, available // sources_remaining)
            source_budget = min(4_000, fair_share, available)

            block = source.as_prompt_block(
                index,
                max_chars=source_budget,
            )
            blocks.append(block)

            remaining -= len(block) + separator_length
            sources_remaining -= 1

        return "\n\n".join(blocks)

    def source_metadata(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "configured": self.configured,
            "searches_attempted": self.searches_attempted,
            "source_count": len(self.sources),
            "sources": [source.metadata_preview() for source in self.sources[:12]],
            "answer": self.answer,
            "error": self.error,
            **self.metadata,
        }


@dataclass(frozen=True)
class ExternalSourceProviderConfig:
    provider: str
    enabled: bool
    reason: Optional[str] = None
    search_limit: int = 3
    timeout_seconds: float = 45.0
    search_depth: str = "basic"
    include_answer: bool = True
    include_raw_content: bool = False
    crawl_max_depth: int = 1
    crawl_limit: int = 20
    crawl_extract_depth: str = "basic"
    research_model: str = "auto"
    research_output_length: str = "standard"

    @classmethod
    def from_env(cls, provider_override: Optional[str] = None, settings: Optional[Mapping[str, str]] = None) -> "ExternalSourceProviderConfig":
        if _env_bool("EXTERNAL_SOURCE_DISABLED", False, settings) or _env_bool("WEB_RESEARCH_DISABLED", False, settings):
            return cls(provider="none", enabled=False, reason="EXTERNAL_SOURCE_DISABLED or WEB_RESEARCH_DISABLED is true.")

        requested = (
            provider_override
            or (settings.get("EXTERNAL_SOURCE_PROVIDER") if settings is not None else config.get("EXTERNAL_SOURCE_PROVIDER"))
            or (settings.get("WEB_RESEARCH_PROVIDER") if settings is not None else config.get("WEB_RESEARCH_PROVIDER"))
            or DEFAULT_EXTERNAL_SOURCE_PROVIDER
        ).strip().lower().replace("_", "-")
        if requested not in SUPPORTED_EXTERNAL_SOURCE_PROVIDERS:
            return cls(provider=requested, enabled=False, reason=f"Unsupported EXTERNAL_SOURCE_PROVIDER {requested!r}.")

        search_limit = _env_int(
            "EXTERNAL_SOURCE_SEARCH_LIMIT",
            _env_int("TAVILY_SEARCH_LIMIT", _env_int("FIRECRAWL_SEARCH_LIMIT", 3, 1, 8, settings), 1, 20, settings),
            1,
            20,
            settings,
        )
        timeout = _env_float(
            "EXTERNAL_SOURCE_TIMEOUT_SECONDS",
            _env_float("TAVILY_TIMEOUT_SECONDS", _env_float("FIRECRAWL_MCP_TIMEOUT_SECONDS", 45.0, 5.0, 180.0, settings), 5.0, 180.0, settings),
            5.0,
            180.0,
            settings,
        )
        get_value = lambda name: settings.get(name) if settings is not None else config.get(name)
        search_depth = (get_value("TAVILY_SEARCH_DEPTH") or "basic").strip().lower() or "basic"
        include_answer = _env_bool("TAVILY_INCLUDE_ANSWER", True, settings)
        include_raw_content = _env_bool("TAVILY_INCLUDE_RAW_CONTENT", False, settings)
        tavily_kwargs = {
            "search_limit": search_limit,
            "timeout_seconds": timeout,
            "search_depth": search_depth,
            "include_answer": include_answer,
            "include_raw_content": include_raw_content,
            "crawl_max_depth": _env_int("TAVILY_CRAWL_MAX_DEPTH", 1, 1, 5, settings),
            "crawl_limit": _env_int("TAVILY_CRAWL_LIMIT", 20, 1, 50, settings),
            "crawl_extract_depth": (get_value("TAVILY_CRAWL_EXTRACT_DEPTH") or "basic").strip().lower() or "basic",
            "research_model": (get_value("TAVILY_RESEARCH_MODEL") or "auto").strip().lower() or "auto",
            "research_output_length": (get_value("TAVILY_RESEARCH_OUTPUT_LENGTH") or "standard").strip().lower() or "standard",
        }

        if requested in {"none", "disabled", "off"}:
            return cls(provider="none", enabled=False, reason="External source research is disabled.")
        if requested == "tavily":
            has_key = bool(_first_env(["TAVILY_API_KEY"], settings))
            return cls(
                provider="tavily",
                enabled=has_key,
                reason=None if has_key else "Set TAVILY_API_KEY to enable Tavily research.",
                **tavily_kwargs,
            )

        firecrawl_config = FirecrawlMCPResearchClient(settings=settings).config
        if requested in {"auto", "firecrawl"} and (requested == "firecrawl" or firecrawl_config.enabled):
            return cls(
                provider="firecrawl",
                enabled=firecrawl_config.enabled,
                reason=firecrawl_config.reason,
                search_limit=firecrawl_config.search_limit or search_limit,
                timeout_seconds=firecrawl_config.timeout_seconds or timeout,
            )
        if requested == "auto" and _first_env(["TAVILY_API_KEY"], settings):
            return cls(provider="tavily", enabled=True, **tavily_kwargs)

        if requested == "firecrawl":
            return cls(
                provider="firecrawl",
                enabled=firecrawl_config.enabled,
                reason=firecrawl_config.reason,
                search_limit=firecrawl_config.search_limit or search_limit,
                timeout_seconds=firecrawl_config.timeout_seconds or timeout,
            )

        return cls(
            provider="firecrawl",
            enabled=False,
            reason="Set FIRECRAWL_API_KEY, FIRECRAWL_MCP_COMMAND, or TAVILY_API_KEY to enable web research.",
            search_limit=search_limit,
            timeout_seconds=timeout,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "enabled": self.enabled,
            "reason": self.reason,
            "search_limit": self.search_limit,
            "timeout_seconds": self.timeout_seconds,
            "search_depth": self.search_depth,
            "include_answer": self.include_answer,
            "include_raw_content": self.include_raw_content,
            "crawl_max_depth": self.crawl_max_depth,
            "crawl_limit": self.crawl_limit,
            "crawl_extract_depth": self.crawl_extract_depth,
            "research_model": self.research_model,
            "research_output_length": self.research_output_length,
        }


class ExternalSourceProvider(ABC):
    provider_name: str

    def __init__(self, config: Optional[ExternalSourceProviderConfig] = None) -> None:
        self.config = config or ExternalSourceProviderConfig.from_env()
        self.provider_name = self.config.provider

    @abstractmethod
    def research(self, queries: Iterable[str]) -> ExternalSourceLibrary:
        raise NotImplementedError

    def get_debug_config(self) -> dict[str, Any]:
        return self.config.as_dict()


class NoExternalSourceProvider(ExternalSourceProvider):
    def research(self, queries: Iterable[str]) -> ExternalSourceLibrary:
        query_list = [query for query in queries if query and query.strip()]
        return ExternalSourceLibrary(
            provider=self.config.provider,
            configured=False,
            searches_attempted=0,
            error=self.config.reason or "External source provider is not configured.",
            metadata={"requested_queries": len(query_list)},
        )


class FirecrawlExternalSourceProvider(ExternalSourceProvider):
    def __init__(self, config: Optional[ExternalSourceProviderConfig] = None, settings: Optional[Mapping[str, str]] = None) -> None:
        super().__init__(config)
        self.client = FirecrawlMCPResearchClient(settings=settings)

    def research(self, queries: Iterable[str]) -> ExternalSourceLibrary:
        result = self.client.research(queries)
        sources = [
            ExternalSourceRecord(
                title=hit.title,
                url=hit.url,
                content=hit.content,
                provider="firecrawl",
                source_type="web",
            )
            for hit in result.hits
        ]
        return ExternalSourceLibrary(
            provider="firecrawl",
            configured=result.configured,
            searches_attempted=result.searches_attempted,
            sources=sources,
            error=result.error,
            metadata={"tool_name": result.tool_name},
        )


def _looks_like_url(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _tavily_content_from_result(value: Mapping[str, Any]) -> str:
    return str(value.get("raw_content") or value.get("content") or value.get("description") or "")


def _tavily_source_record(value: Mapping[str, Any], *, source_type: str = "web") -> ExternalSourceRecord:
    score = value.get("score")
    try:
        normalized_score = float(score) if score is not None else None
    except (TypeError, ValueError):
        normalized_score = None
    return ExternalSourceRecord(
        title=str(value.get("title") or value.get("url") or ""),
        url=str(value.get("url") or ""),
        content=_tavily_content_from_result(value),
        source_type=str(value.get("source_type") or source_type),
        provider="tavily",
        score=normalized_score,
        published_at=str(value.get("published_date") or value.get("published_at") or "") or None,
        metadata={
            key: item
            for key, item in value.items()
            if key not in {"title", "url", "content", "raw_content", "score", "published_date", "published_at"}
        },
    )


class TavilyExternalSourceProvider(ExternalSourceProvider):
    def __init__(self, config: Optional[ExternalSourceProviderConfig] = None, settings: Optional[Mapping[str, str]] = None) -> None:
        super().__init__(config)
        self.settings = settings

    def research(self, queries: Iterable[str]) -> ExternalSourceLibrary:
        if not self.config.enabled:
            return ExternalSourceLibrary(provider="tavily", configured=False, error=self.config.reason)

        api_key = _first_env(["TAVILY_API_KEY"], self.settings)
        if not api_key:
            return ExternalSourceLibrary(provider="tavily", configured=False, error="Set TAVILY_API_KEY to enable Tavily research.")

        query_list = [query.strip() for query in queries if query and query.strip()]
        if not query_list:
            return ExternalSourceLibrary(provider="tavily", configured=True, error="No research queries were provided.")

        try:
            from tavily import TavilyClient
        except ImportError as exc:
            return ExternalSourceLibrary(
                provider="tavily",
                configured=False,
                error="tavily-python is required for Tavily research. Install with `pip install tavily-python`.",
                metadata={"import_error": str(exc)},
            )

        try:
            client = TavilyClient(api_key=api_key)
            sources: list[ExternalSourceRecord] = []
            answer: Optional[str] = None
            searches_attempted = 0
            tools_used: list[str] = []
            search_depth = "advanced" if self.config.research_model in {"pro"} else self.config.search_depth
            for query in query_list:
                searches_attempted += 1
                if _looks_like_url(query):
                    tools_used.append("crawl")
                    response = client.crawl(
                        url=query,
                        max_depth=self.config.crawl_max_depth,
                        limit=self.config.crawl_limit,
                        extract_depth=self.config.crawl_extract_depth,
                    )
                    raw_results = response.get("results") if isinstance(response, Mapping) else None
                    if isinstance(raw_results, list):
                        for item in raw_results:
                            if isinstance(item, Mapping):
                                sources.append(_tavily_source_record(item, source_type="crawl"))
                    continue

                tools_used.append("search")
                response = client.search(
                    query=query,
                    search_depth=search_depth,
                    max_results=self.config.search_limit,
                    include_answer=self.config.include_answer,
                    include_raw_content=self.config.include_raw_content,
                )
                if isinstance(response, Mapping):
                    if not answer and isinstance(response.get("answer"), str):
                        answer = response["answer"]
                    raw_results = response.get("results")
                    if isinstance(raw_results, list):
                        for item in raw_results:
                            if isinstance(item, Mapping):
                                sources.append(_tavily_source_record(item))

            deduped = _dedupe_sources(sources, max_items=max(self.config.search_limit, self.config.crawl_limit) * 4)
            return ExternalSourceLibrary(
                provider="tavily",
                configured=True,
                searches_attempted=searches_attempted,
                sources=deduped,
                answer=answer,
                metadata={
                    "search_depth": search_depth,
                    "research_model": self.config.research_model,
                    "research_output_length": self.config.research_output_length,
                    "tools": tools_used,
                },
            )
        except Exception as exc:
            return ExternalSourceLibrary(
                provider="tavily",
                configured=True,
                searches_attempted=len(query_list),
                error=str(exc),
            )


def _dedupe_sources(sources: list[ExternalSourceRecord], *, max_items: int) -> list[ExternalSourceRecord]:
    seen: set[str] = set()
    deduped: list[ExternalSourceRecord] = []
    for source in sources:
        key = (source.url or source.title or source.content[:80]).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(source)
    return deduped[:max_items]


def build_external_source_provider(
    config: Optional[ExternalSourceProviderConfig] = None,
    *,
    provider: Optional[str] = None,
    settings: Optional[Mapping[str, str]] = None,
) -> ExternalSourceProvider:
    resolved = config or ExternalSourceProviderConfig.from_env(provider_override=provider, settings=settings)
    if not resolved.enabled:
        return NoExternalSourceProvider(resolved)
    if resolved.provider == "tavily":
        return TavilyExternalSourceProvider(resolved, settings=settings)
    if resolved.provider == "firecrawl":
        return FirecrawlExternalSourceProvider(resolved, settings=settings)
    return NoExternalSourceProvider(resolved)


__all__ = [
    "ExternalSourceLibrary",
    "ExternalSourceProvider",
    "ExternalSourceProviderConfig",
    "ExternalSourceRecord",
    "FirecrawlExternalSourceProvider",
    "NoExternalSourceProvider",
    "SUPPORTED_EXTERNAL_SOURCE_PROVIDERS",
    "TavilyExternalSourceProvider",
    "build_external_source_provider",
]
