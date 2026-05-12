from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
RECENT_LIST_URL = "https://arxiv.org/list/cond-mat/recent"

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "have",
    "has", "in", "into", "is", "it", "its", "of", "on", "or", "that", "the",
    "their", "this", "to", "was", "we", "with", "via", "using", "use",
    "our", "these", "those", "which", "can", "may", "not", "than", "into",
    "over", "under", "new", "study", "paper", "show", "shows", "showing",
    "result", "results", "method", "methods", "based",
}

# ── arXiv recent-list HTML parser ──────────────────────────────

class ArxivRecentListParser(HTMLParser):
    def __init__(self, listing_days: int) -> None:
        super().__init__()
        self.listing_days = listing_days
        self.current_section = -1
        self.collecting_done = False
        self.in_h3 = False
        self.h3_buffer: list[str] = []
        self.sections: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h3":
            self.in_h3 = True
            self.h3_buffer = []
            return
        if self.collecting_done or tag != "a" or self.current_section < 0 or self.current_section >= self.listing_days:
            return
        href = dict(attrs).get("href", "")
        if not href.startswith("/abs/"):
            return
        arxiv_id = href.rsplit("/", 1)[-1]
        ids = self.sections[self.current_section]["ids"]
        if isinstance(ids, list) and arxiv_id not in ids:
            ids.append(arxiv_id)

    def handle_endtag(self, tag: str) -> None:
        if tag != "h3" or not self.in_h3:
            return
        title = " ".join("".join(self.h3_buffer).split())
        self.in_h3 = False
        if "showing" in title and "entries" in title:
            if len(self.sections) < self.listing_days:
                self.sections.append({"title": title, "ids": []})
                self.current_section = len(self.sections) - 1
            else:
                self.collecting_done = True
                self.current_section = -1

    def handle_data(self, data: str) -> None:
        if self.in_h3:
            self.h3_buffer.append(data)


# ── Data classes ────────────────────────────────────────────────

@dataclass
class Paper:
    arxiv_id: str
    title: str
    authors: list[str]
    published: str
    updated: str
    primary_category: str
    categories: list[str]
    abstract: str
    study_overview_zh: str = ""
    abstract_summary_zh: str = ""
    main_content_zh: str = ""
    method_zh: str = ""
    summary_mode: str = ""
    keywords: list[str] = field(default_factory=list)
    pdf_url: str = ""
    abs_url: str = ""


# ── arXiv API helpers ──────────────────────────────────────────

def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def build_query(categories: list[str]) -> str:
    category_expr = " OR ".join(f"cat:{cat}" for cat in categories)
    return f"({category_expr})" if category_expr else "all:*"


def http_get(url: str, timeout: int = 30) -> bytes:
    request = Request(url, headers={"User-Agent": "codex-arxiv-daily/1.0"})
    if url.startswith("https://"):
        context = ssl.create_default_context()
        with urlopen(request, timeout=timeout, context=context) as response:
            return response.read()
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_feed(query: str, start: int, max_results: int) -> ET.Element:
    params = {
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": str(start),
        "max_results": str(max_results),
    }
    urls = [
        f"https://export.arxiv.org/api/query?{urlencode(params, quote_via=quote)}",
        f"http://export.arxiv.org/api/query?{urlencode(params, quote_via=quote)}",
    ]
    last_error: Exception | None = None
    for url in urls:
        try:
            return ET.fromstring(http_get(url))
        except (URLError, TimeoutError, ET.ParseError) as exc:
            last_error = exc
    raise RuntimeError(f"Failed to fetch arXiv feed: {last_error}")


def fetch_feed_by_ids(ids: list[str]) -> ET.Element:
    params = {
        "id_list": ",".join(ids),
        "start": "0",
        "max_results": str(len(ids)),
    }
    urls = [
        f"https://export.arxiv.org/api/query?{urlencode(params, quote_via=quote)}",
        f"http://export.arxiv.org/api/query?{urlencode(params, quote_via=quote)}",
    ]
    last_error: Exception | None = None
    for url in urls:
        try:
            return ET.fromstring(http_get(url))
        except (URLError, TimeoutError, ET.ParseError) as exc:
            last_error = exc
    raise RuntimeError(f"Failed to fetch arXiv feed by ids: {last_error}")


def fetch_recent_listing_ids(config: dict) -> tuple[list[str], list[dict[str, object]]]:
    listing_days = int(config.get("listing_days", 3))
    show = int(config.get("recent_list_show", 1000))
    url = f"{RECENT_LIST_URL}?show={show}"
    html = http_get(url).decode("utf-8", errors="replace")
    parser = ArxivRecentListParser(listing_days=listing_days)
    parser.feed(html)
    ids: list[str] = []
    seen: set[str] = set()
    for section in parser.sections:
        for arxiv_id in section.get("ids", []):
            if isinstance(arxiv_id, str) and arxiv_id not in seen:
                seen.add(arxiv_id)
                ids.append(arxiv_id)
    return ids, parser.sections


# ── XML helpers ─────────────────────────────────────────────────

def text_of(element: ET.Element | None, tag: str, default: str = "") -> str:
    if element is None:
        return default
    child = element.find(tag)
    if child is None or child.text is None:
        return default
    return " ".join(child.text.split())


def entry_text(entry: ET.Element, tag: str, default: str = "") -> str:
    child = entry.find(f"{ATOM_NS}{tag}")
    if child is None or child.text is None:
        return default
    return " ".join(child.text.split())


def parse_authors(entry: ET.Element) -> list[str]:
    authors = []
    for author in entry.findall(f"{ATOM_NS}author"):
        name = text_of(author, f"{ATOM_NS}name")
        if name:
            authors.append(name)
    return authors


def parse_categories(entry: ET.Element) -> tuple[str, list[str]]:
    primary = ""
    primary_category = entry.find(f"{ARXIV_NS}primary_category")
    if primary_category is not None:
        primary = primary_category.attrib.get("term", "")
    categories = [
        category.attrib.get("term", "")
        for category in entry.findall(f"{ATOM_NS}category")
        if category.attrib.get("term", "")
    ]
    if primary and primary not in categories:
        categories.insert(0, primary)
    return primary, categories


# ── Text helpers ────────────────────────────────────────────────

def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    return [part.strip() for part in parts if part.strip()]


def trim_text(text: str, limit: int = 180) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def has_any(text: str, terms: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def escape_json_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", "")


# ── Improved study type classification ─────────────────────────

def infer_study_kind(title: str, abstract: str) -> str:
    """Weighted keyword voting to classify paper type."""
    combined = f"{title}. {abstract}".lower()

    strong_exp = [
        "measured", "synthesized", "grown", "fabricated", "spectroscopy",
        "arpes", "transport measurements", "magnetization measurement",
        "heat capacity", "x-ray diffraction", "neutron scattering",
        "raman spectroscopy", "scanning tunneling", "atomic force microscopy",
        "transmission electron", "angle-resolved photoemission",
    ]
    weak_exp = [
        "experiment", "experimental", "observed", "observation",
        "sample", "samples", "measurements", "measured",
    ]
    strong_comp = [
        "density functional theory", "first-principles", "first principles",
        "ab initio", "molecular dynamics", "monte carlo",
        "exact diagonalization", "dmrg", "tensor network",
        "finite element method", "machine learning", "deep learning",
        "neural network", "dft calculations", "dft+u", "gw approximation",
        "dynamical mean-field theory",
    ]
    weak_comp = [
        "simulation", "simulations", "numerical", "computational",
        "calculated", "we calculate", "we compute",
    ]
    strong_theory = [
        "field theory", "renormalization group", "conformal field theory",
        "topological field theory", "mean-field theory", "kubo formula",
        "luttinger liquid", "fermi liquid theory", "gauge theory",
        "scaling theory", "effective field theory", "landau theory",
        "bethe ansatz", "conformal bootstrap",
    ]
    weak_theory = [
        "theory", "theoretical", "hamiltonian", "we derive",
        "derive", "model", "lagrangian", "analytical",
        "analytic", "equation", "formalism",
    ]

    score_exp = sum(3 for t in strong_exp if t in combined) + sum(1 for t in weak_exp if t in combined)
    score_comp = sum(3 for t in strong_comp if t in combined) + sum(1 for t in weak_comp if t in combined)
    score_theory = sum(3 for t in strong_theory if t in combined) + sum(1 for t in weak_theory if t in combined)

    kinds = []
    if score_exp >= 3:
        kinds.append("实验")
    elif score_exp >= 1:
        kinds.append("实验")
    if score_comp >= 3:
        kinds.append("计算")
    elif score_comp >= 1:
        kinds.append("计算")
    if score_theory >= 3:
        kinds.append("理论")
    elif score_theory >= 1:
        kinds.append("理论")

    if len(kinds) == 0:
        # try to guess from first sentence patterns
        first = combined.split(".")[0] if "." in combined else combined
        if has_any(first, ["experimentally", "we measure", "we observe"]):
            return "实验文章"
        if has_any(first, ["we simulate", "we compute", "using dft"]):
            return "计算文章"
        if has_any(first, ["we study", "we consider", "we investigate"]):
            return "理论文章"
        return "分类不明确"

    unique = list(dict.fromkeys(kinds))  # preserve order, deduplicate
    if len(unique) == 1:
        return f"{unique[0]}文章"
    return " + ".join(unique) + " 文章"


# ── Improved research object extraction ────────────────────────

def infer_research_object(title: str, abstract: str) -> str:
    """
    Extract the actual research object (material, system, phenomenon)
    from title + abstract using layered patterns.
    """
    combined = f"{title}. {abstract}"

    # Pattern 1: "We study/investigate/examine X" (most reliable)
    m = re.search(
        r"(?:stud(?:y|ies)|investigat(?:e|es)|examin(?:e|es)|explor(?:e|es)|"
        r"analyz(?:e|es)|consider(?:s)|present(?:\s+a)?)\s+(?:the\s+)?"
        r"([A-Z][\w\s\-\{\}\$\\,'\"\(\)\[\]/+*]{3,120}?)(?:\.|,|;|\s+and\s+|\s+in\s+|\s+via\s+|\s+using\s+|\s+with\s+|\s+for\s+)",
        combined
    )
    if m:
        obj = m.group(1).strip(" .,;:()")
        if 3 <= len(obj) <= 120:
            # Strip leading articles/determiners
            obj = re.sub(r'^(the|a|an|this|these|those|our|their|its|such)\s+', '', obj, flags=re.IGNORECASE)
            if not obj.lower().startswith(("the ", "a ", "an ")):
                return trim_text(obj, 100)

    # Pattern 2: "of X" after material-related words
    m = re.search(
        r"(?:properties|behavior|physics|dynamics|phases?|structure|"
        r"transport|transitions?|states?|effects?)\s+of\s+"
        r"([A-Z][\w\s\-\{\}\$\\,'\"\(\)\[\]/+*]{3,100}?)(?:\.|,|;|\s+in\s+|\s+via\s+|\s+using\s+)",
        combined, re.IGNORECASE
    )
    if m:
        obj = m.group(1).strip(" .,;:()")
        if 3 <= len(obj) <= 100:
            return trim_text(obj, 100)

    # Pattern 3: Material/compound names: chemical formulas or proper nouns
    # e.g. "BaTiO3", "graphene", "MoS2", "YBa2Cu3O7"
    material_matches = re.findall(
        r'\b([A-Z][a-z]?[0-9]?(?:[A-Z][a-z]?[0-9]?){1,6}(?:\s+(?:films?|crystals?|nanowires?|quantum\s+dots?|heterostructures?|monolayers?|bilayers?))?)\b',
        combined
    )
    if material_matches:
        # pick the most specific one (longest match, excluding common words)
        filtered = [m for m in material_matches
                    if len(m) > 3 and m.lower() not in
                    {"the", "this", "that", "these", "those", "with", "from", "their", "which"}]
        if filtered:
            return trim_text(max(filtered, key=len), 100)

    # Pattern 4: Physics phenomenon descriptions
    m = re.search(
        r'(?:phenomen(?:on|a)|effect|transition|phase|state)\s+(?:of|in|called|known\s+as)\s+'
        r'([A-Z][\w\s\-\{\}\$\\,\'\"\(\)\[\]/+*]{3,100}?)(?:\.|,|;)',
        combined, re.IGNORECASE
    )
    if m:
        return trim_text(m.group(1).strip(" .,;:"), 100)

    # Fallback: first significant noun phrase from title
    title_clean = re.sub(r'\$[^$]+\$', '', title)  # remove math
    title_clean = re.sub(r'[\(\[\{].*?[\)\]\}]', '', title_clean)  # remove parentheticals
    title_clean = " ".join(title_clean.split())
    if len(title_clean) > 5:
        return trim_text(title_clean, 100)

    return "未能从摘要明确提取研究对象"


# ── Improved method extraction ──────────────────────────────────

def extract_method(title: str, abstract: str, kind: str) -> str:
    """
    Extract the actual research method from abstract.
    Uses multiple indicators, not just "using/via/by".
    """
    combined = f"{title}. {abstract}"

    # Method-indicating phrases with their context window
    method_indicators = [
        r'(?:using|via|by\s+means\s+of|through|employing|utilizing|applying)\s+([^.;]{5,150}?)(?:\.|,|;|\s+to\s+|\s+and\s+(?:we|the|our|this|these|show|find|observe|demonstrate|investigate|study))',
        r'(?:measured|characterized|performed|conducted|carried\s+out)\s+(?:using|via|by|with)\s+([^.;]{5,150}?)(?:\.|,|;)',
        r'(?:method|technique|approach|setup)\s+(?:is|was|employed|used|based\s+on|utilized)\s+(?:is|was|to\s+)?([^.;]{5,150}?)(?:\.|,|;)',
        r'(?:we\s+(?:perform|carry\s+out|conduct|employ|use|utilize|apply))\s+([^.;]{5,150}?)(?:\.|,|;|\s+to\s+|\s+and\s+(?:we|the|our|this|these|show|find|observe|demonstrate))',
    ]

    for pattern in method_indicators:
        m = re.search(pattern, combined, re.IGNORECASE)
        if m:
            method_text = m.group(1).strip(" .,;:()")
            # filter out obviously wrong extractions
            if len(method_text) < 5:
                continue
            if re.match(r'^(the|a|an|our|this|these|those|its|their|such)\s+', method_text.lower()):
                continue
            # filter challenges/limitations that happen to follow "using/via/by"
            if has_any(method_text, ["limited", "challenges", "difficult", "not possible",
                                      "lack of", "absence of", "hard to", "remains"]):
                continue
            return trim_text(method_text, 150)

    # Specific method patterns for known techniques
    specific_methods = [
        (r'\b(DFT|density[\s-]functional[\s-]theory)\b', '密度泛函理论 (DFT) 计算'),
        (r'\b(DMRG|density[\s-]matrix[\s-]renormalization[\s-]group)\b', '密度矩阵重整化群 (DMRG)'),
        (r'\b(Monte\s+Carlo|QMC|quantum\s+Monte\s+Carlo)\b', '蒙特卡洛模拟'),
        (r'\b(molecular\s+dynamics|MD\s+simulation)\b', '分子动力学模拟'),
        (r'\b(ARPES|angle[\s-]resolved[\s-]photoemission)\b', '角分辨光电子能谱 (ARPES)'),
        (r'\b(STM|scanning[\s-]tunneling[\s-]microscop)\b', '扫描隧道显微镜 (STM)'),
        (r'\b(neutron\s+(?:scattering|diffraction))\b', '中子散射/衍射'),
        (r'\b(Raman\s+spectroscopy)\b', '拉曼光谱'),
        (r'\b(XRD|X-ray\s+diffraction)\b', 'X射线衍射 (XRD)'),
        (r'\b(TEM|transmission\s+electron\s+microscop)\b', '透射电子显微镜 (TEM)'),
        (r'\b(machine\s+learning|deep\s+learning|neural\s+network)\b', '机器学习方法'),
        (r'\b(tensor\s+network)\b', '张量网络方法'),
        (r'\b(exact\s+diagonalization)\b', '严格对角化'),
        (r'\b(dynamical\s+mean[\s-]field\s+theory|DMFT)\b', '动力学平均场理论 (DMFT)'),
        (r'\b(GW\s+approximation|GW\s+calculations?)\b', 'GW近似计算'),
        (r'\b(EPR|electron\s+paramagnetic\s+resonance|ESR)\b', '电子顺磁共振 (EPR/ESR)'),
        (r'\b(NMR|nuclear\s+magnetic\s+resonance)\b', '核磁共振 (NMR)'),
        (r'\b(ellipsometry|spectroscopic\s+ellipsometry)\b', '椭圆偏振光谱'),
    ]

    for pattern, label in specific_methods:
        if re.search(pattern, combined, re.IGNORECASE):
            return label

    if "实验" in kind:
        return "摘要未明确说明具体实验手段"
    if "计算" in kind:
        return "摘要未明确说明具体计算方法"
    if "理论" in kind:
        return "摘要未明确说明具体理论方法"
    return "摘要未明确说明具体方法"


# ── Extract keywords ───────────────────────────────────────────

def extract_keywords(text: str, limit: int = 8) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-']+", text.lower())
    counts = Counter(word for word in words if len(word) > 2 and word not in STOPWORDS)
    return [word for word, _ in counts.most_common(limit)]


# ── Improved rule-based fallback summary ───────────────────────

def fallback_chinese_summary(title: str, abstract: str) -> tuple[dict[str, str], list[str]]:
    keywords = extract_keywords(f"{title} {abstract}")
    kind = infer_study_kind(title, abstract)
    research_object = infer_research_object(title, abstract)
    method = extract_method(title, abstract, kind)

    # Build a more informative abstract summary from key sentences
    sentences = split_sentences(abstract)
    key_sentences = sentences[:min(3, len(sentences))]
    abstract_gist = "。".join(trim_text(s, 200) for s in key_sentences)

    # Determine topic domain from keywords
    domain_hints = []
    kw_lower = [k.lower() for k in keywords]
    if has_any(" ".join(kw_lower), ["superconduct", "superconducting", "superconductor"]):
        domain_hints.append("超导")
    if has_any(" ".join(kw_lower), ["magnetic", "magnetism", "spin", "ferromagnetic", "antiferromagnetic"]):
        domain_hints.append("磁性")
    if has_any(" ".join(kw_lower), ["topological", "topology", "chern", "berry"]):
        domain_hints.append("拓扑")
    if has_any(" ".join(kw_lower), ["graphene", "mos2", "hbn", "tmdc", "transition metal dichalcogenide"]):
        domain_hints.append("二维材料")
    if has_any(" ".join(kw_lower), ["quantum", "entanglement", "qubit"]):
        domain_hints.append("量子")
    if has_any(" ".join(kw_lower), ["phonon", "thermal", "heat", "temperature"]):
        domain_hints.append("热/声子")
    if has_any(" ".join(kw_lower), ["optical", "photon", "exciton", "polariton"]):
        domain_hints.append("光学/激子")
    if has_any(" ".join(kw_lower), ["disorder", "localization", "anderson"]):
        domain_hints.append("无序/局域化")
    if has_any(" ".join(kw_lower), ["bose", "cold atom", "optical lattice"]):
        domain_hints.append("冷原子")
    if has_any(" ".join(kw_lower), ["soft matter", "polymer", "colloid", "liquid crystal"]):
        domain_hints.append("软物质")
    if has_any(" ".join(kw_lower), ["machine learning", "neural network", "deep learning"]):
        domain_hints.append("机器学习")

    domain_str = "、".join(domain_hints) if domain_hints else "凝聚态物理"
    topic_str = "、".join(keywords[:4]) if keywords else "相关物理问题"

    # Build the study overview
    overview_parts = [f"这是一篇{kind}"]
    overview_parts.append(f"研究对象为 {research_object}")
    if "实验" in kind and "未明确说明" not in method:
        overview_parts.append(f"主要实验手段：{method}")
    elif "计算" in kind and "未明确说明" not in method:
        overview_parts.append(f"主要计算方法：{method}")
    elif "理论" in kind and "未明确说明" not in method:
        overview_parts.append(f"主要理论方法：{method}")

    study_overview = "；".join(overview_parts) + "。"

    summary = {
        "study_overview_zh": study_overview,
        "abstract_summary_zh": f"摘要要点：{abstract_gist}",
        "main_content_zh": f"本文属于{domain_str}领域，核心关注 {topic_str}。{abstract_gist}",
        "method_zh": method + ("。" if not method.endswith("。") else ""),
        "summary_mode": "rule-based",
    }
    return summary, keywords


# ── LLM summarization (OpenAI / DeepSeek / compatible) ─────────

def _build_summary_prompt(title: str, abstract: str) -> str:
    """Build the system + user prompt for LLM summarization."""
    system_prompt = (
        "你是一位凝聚态物理博士后，正在为每日 arXiv 论文写中文阅读笔记。"
        "请严格根据提供的论文标题和英文摘要进行总结，绝对不要编造摘要中没有的信息。"
        "如果摘要中缺乏某方面信息，请诚实地写「摘要未明确说明」。"
    )
    user_prompt = f"""请分析以下凝聚态物理论文，用中文给出结构化总结。

论文标题：{title}

论文摘要：{abstract}

请按以下格式输出（每项用中文，简洁准确）：

【研究类型】判断这是实验文章、计算文章、理论文章还是混合类型。给出判断依据的关键词。
【研究对象】论文研究的具体材料、体系或物理现象是什么？例如：特定化合物、异质结、量子系统、相变、输运现象等。
【核心发现】用1-2句话概括论文最重要的物理结论或发现。
【研究方法】具体用了什么实验手段、计算方法或理论框架？尽量具体（如 DFT+U、DMRG、ARPES、分子束外延等），而不是泛泛说"实验方法"。
【创新点】这篇工作相对于已有研究的主要新意在哪里？（若摘要中不明显，可写"摘要未突出说明"）"""
    return f"{system_prompt}\n\n{user_prompt}"


def _parse_llm_response(text: str) -> dict[str, str]:
    """Parse the LLM's Chinese response into structured fields."""
    fields = {
        "研究类型": "study_type_zh",
        "研究对象": "research_object_zh",
        "核心发现": "core_finding_zh",
        "研究方法": "method_zh",
        "创新点": "novelty_zh",
    }
    result: dict[str, str] = {}
    for cn_label, key in fields.items():
        # Match patterns like "【研究类型】xxx" or "研究类型：xxx" or "研究类型: xxx"
        m = re.search(
            rf'(?:【{cn_label}】|{cn_label}[：:])\s*(.+?)(?:\n|$)',
            text, re.MULTILINE
        )
        result[key] = m.group(1).strip() if m else ""

    # If no structured format found, use entire response as fallback
    if not any(result.values()):
        result["study_type_zh"] = text[:200]

    return result


def summarize_with_llm(title: str, abstract: str, config: dict) -> dict[str, str]:
    """Summarize using an LLM (OpenAI, DeepSeek, or any OpenAI-compatible API)."""
    llm_cfg = config.get("llm", {})
    provider = llm_cfg.get("provider", "openai")
    api_key_env = llm_cfg.get("api_key_env", "OPENAI_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        # fallback: try OPENAI_API_KEY
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(f"API key not found in env var {api_key_env} or OPENAI_API_KEY")

    model = llm_cfg.get("model", config.get("openai_model", "gpt-4.1-mini"))
    base_url = llm_cfg.get("base_url", "https://api.openai.com/v1")
    timeout = int(llm_cfg.get("timeout", 60))

    # Ensure base_url ends with /chat/completions or add it
    api_url = base_url.rstrip("/")
    if not api_url.endswith("/chat/completions"):
        api_url += "/chat/completions"

    prompt = _build_summary_prompt(title, abstract)

    body = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 800,
        "temperature": 0.1,
    }

    request = Request(
        api_url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "codex-arxiv-daily/2.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM request failed: {exc.code} {detail}") from exc

    # Extract response text (OpenAI chat completions format)
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Unexpected LLM response format: {json.dumps(payload)[:300]}")

    parsed = _parse_llm_response(text)
    parsed["summary_mode"] = f"llm-{provider}"

    # Map to the expected output fields
    result = {
        "study_overview_zh": (
            f"研究类型：{parsed.get('study_type_zh', '未分类')}；"
            f"研究对象：{parsed.get('research_object_zh', '未识别')}"
        ),
        "abstract_summary_zh": parsed.get("core_finding_zh", text[:300]),
        "main_content_zh": (
            f"核心发现：{parsed.get('core_finding_zh', '见摘要概括')}；"
            f"创新点：{parsed.get('novelty_zh', '摘要未突出说明')}"
        ),
        "method_zh": parsed.get("method_zh", "摘要未明确说明"),
        "summary_mode": f"llm-{provider}",
        "_llm_raw": text,  # keep raw for debugging
    }
    return result


# ── Main summarization dispatcher ──────────────────────────────

def make_chinese_summary(title: str, abstract: str, config: dict) -> tuple[dict[str, str], list[str]]:
    fallback, keywords = fallback_chinese_summary(title, abstract)

    if not config.get("use_openai_summary", True):
        return fallback, keywords

    # Check if any LLM API key is available
    llm_cfg = config.get("llm", {})
    api_key_env = llm_cfg.get("api_key_env", "OPENAI_API_KEY")
    has_key = bool(os.environ.get(api_key_env) or os.environ.get("OPENAI_API_KEY"))

    if not has_key:
        return fallback, keywords

    try:
        summary = summarize_with_llm(title, abstract, config)
        time.sleep(0.1)  # rate limiting
        return summary, keywords
    except Exception as exc:
        fallback["summary_mode"] = f"rule-based; llm_error={trim_text(str(exc), 120)}"
        return fallback, keywords


# ── Paper cache for resume support ─────────────────────────────

class PaperCache:
    """Thread-safe cache for paper summaries."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, dict] = {}
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, arxiv_id: str) -> dict | None:
        with self.lock:
            return self.data.get(arxiv_id)

    def set(self, arxiv_id: str, summary: dict, keywords: list[str]) -> None:
        with self.lock:
            self.data[arxiv_id] = {
                "summary": summary,
                "keywords": keywords,
            }

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data_copy = dict(self.data)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(data_copy, f, ensure_ascii=False, indent=2)


# ── Entry parsing ──────────────────────────────────────────────

def parse_entry(entry: ET.Element, config: dict, cache: PaperCache | None = None) -> Paper:
    title = entry_text(entry, "title")
    abstract = entry_text(entry, "summary")
    published = entry_text(entry, "published")
    updated = entry_text(entry, "updated")
    authors = parse_authors(entry)
    primary_category, categories = parse_categories(entry)
    arxiv_id = entry_text(entry, "id").rsplit("/", 1)[-1]

    # Check cache first
    if cache:
        cached = cache.get(arxiv_id)
        if cached and cached.get("summary", {}).get("summary_mode", "").startswith("llm-"):
            summary = cached["summary"]
            keywords = cached["keywords"]
        else:
            summary, keywords = make_chinese_summary(title, abstract, config)
            cache.set(arxiv_id, summary, keywords)
    else:
        summary, keywords = make_chinese_summary(title, abstract, config)

    link_pdf = ""
    link_abs = ""
    for link in entry.findall(f"{ATOM_NS}link"):
        href = link.attrib.get("href", "")
        rel = link.attrib.get("rel", "")
        title_attr = link.attrib.get("title", "")
        if rel == "alternate":
            link_abs = href
        if rel == "related" or title_attr.lower() == "pdf":
            link_pdf = href
    if not link_abs:
        link_abs = f"https://arxiv.org/abs/{arxiv_id}"
    if not link_pdf and link_abs:
        link_pdf = link_abs.replace("/abs/", "/pdf/") + ".pdf"

    return Paper(
        arxiv_id=arxiv_id,
        title=title,
        authors=authors,
        published=published,
        updated=updated,
        primary_category=primary_category,
        categories=categories,
        abstract=abstract,
        study_overview_zh=summary["study_overview_zh"],
        abstract_summary_zh=summary["abstract_summary_zh"],
        main_content_zh=summary["main_content_zh"],
        method_zh=summary["method_zh"],
        summary_mode=summary["summary_mode"],
        keywords=keywords,
        pdf_url=link_pdf,
        abs_url=link_abs,
    )


# ── Serialization ──────────────────────────────────────────────

def safe_iso(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return value


def serialize_papers(papers: Iterable[Paper]) -> list[dict]:
    items = []
    for paper in papers:
        items.append({
            "arxiv_id": paper.arxiv_id,
            "title": paper.title,
            "authors": paper.authors,
            "published": safe_iso(paper.published),
            "updated": safe_iso(paper.updated),
            "primary_category": paper.primary_category,
            "categories": paper.categories,
            "abstract": paper.abstract,
            "study_overview_zh": paper.study_overview_zh,
            "abstract_summary_zh": paper.abstract_summary_zh,
            "main_content_zh": paper.main_content_zh,
            "method_zh": paper.method_zh,
            "summary_mode": paper.summary_mode,
            "keywords": paper.keywords,
            "pdf_url": paper.pdf_url,
            "abs_url": paper.abs_url,
        })
    return items


# ── Fetching ───────────────────────────────────────────────────

def normalize_arxiv_id(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", arxiv_id)


def _process_batch(entries: list[ET.Element], config: dict, cache: PaperCache,
                   lock: threading.Lock, stats: dict) -> list[Paper]:
    papers = []
    for entry in entries:
        try:
            paper = parse_entry(entry, config, cache)
            papers.append(paper)
            mode = paper.summary_mode
            with lock:
                stats[mode] = stats.get(mode, 0) + 1
        except Exception as exc:
            # If one paper fails, still process the rest
            title = entry_text(entry, "title", "unknown")
            print(f"  [WARN] Failed to process '{title[:60]}...': {exc}")
    return papers


def fetch_papers_by_listing(config: dict) -> tuple[list[Paper], list[dict[str, object]]]:
    ids, sections = fetch_recent_listing_ids(config)
    papers_by_id: dict[str, Paper] = {}

    cache_dir = Path(config.get("site_data_path", "arxiv-daily/site/data/latest.json")).parent
    cache_path = cache_dir / ".paper_cache.json"
    cache = PaperCache(cache_path)

    batch_size = 50
    max_workers = int(config.get("llm", {}).get("max_concurrent", 3))
    lock = threading.Lock()
    stats: dict[str, int] = {}
    total = len(ids)

    print(f"Fetching details for {total} papers (batch size={batch_size}, workers={max_workers})...")

    if max_workers > 1 and total > batch_size:
        # Parallel fetching with ThreadPoolExecutor
        all_papers: list[Paper] = []
        for start in range(0, total, batch_size):
            batch_ids = ids[start:start + batch_size]
            try:
                feed = fetch_feed_by_ids(batch_ids)
            except Exception as exc:
                print(f"  [WARN] Failed to fetch batch {start}-{start + batch_size}: {exc}")
                continue
            entries = feed.findall(f"{ATOM_NS}entry")

            # Split entries into sub-batches for parallel LLM processing
            sub_batch_size = max(5, len(entries) // max_workers)
            sub_batches = [entries[i:i + sub_batch_size] for i in range(0, len(entries), sub_batch_size)]

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(_process_batch, sub, config, cache, lock, stats)
                    for sub in sub_batches
                ]
                for future in as_completed(futures):
                    all_papers.extend(future.result())

            cache.save()  # save after each batch
            done = min(start + batch_size, total)
            llm_count = sum(v for k, v in stats.items() if k.startswith("llm-"))
            rule_count = sum(v for k, v in stats.items() if k.startswith("rule"))
            print(f"  Progress: {done}/{total} | LLM: {llm_count} | Rule: {rule_count}")

            if start + batch_size < total:
                time.sleep(0.3)  # be nice to arXiv API

        # Build ordered results
        for paper in all_papers:
            papers_by_id[normalize_arxiv_id(paper.arxiv_id)] = paper
    else:
        # Sequential processing
        for start in range(0, total, batch_size):
            batch_ids = ids[start:start + batch_size]
            try:
                feed = fetch_feed_by_ids(batch_ids)
            except Exception as exc:
                print(f"  [WARN] Failed to fetch batch {start}-{start + batch_size}: {exc}")
                continue
            entries = feed.findall(f"{ATOM_NS}entry")
            for entry in entries:
                try:
                    paper = parse_entry(entry, config, cache)
                    papers_by_id[normalize_arxiv_id(paper.arxiv_id)] = paper
                    mode = paper.summary_mode
                    stats[mode] = stats.get(mode, 0) + 1
                except Exception as exc:
                    title = entry_text(entry, "title", "unknown")
                    print(f"  [WARN] Failed to process '{title[:60]}...': {exc}")
            cache.save()
            done = min(start + batch_size, total)
            print(f"  Progress: {done}/{total}")
            time.sleep(0.1)

    cache.save()

    # Summary stats
    llm_count = sum(v for k, v in stats.items() if k.startswith("llm-"))
    rule_count = sum(v for k, v in stats.items() if k.startswith("rule"))
    print(f"Summarization: {llm_count} LLM, {rule_count} rule-based")

    papers = [papers_by_id[normalize_arxiv_id(arxiv_id)]
              for arxiv_id in ids
              if normalize_arxiv_id(arxiv_id) in papers_by_id]
    return papers, sections


# ── Ensure directory exists ────────────────────────────────────

def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


# ── Main ───────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch recent arXiv papers into a static JSON file.")
    parser.add_argument("--config", default="arxiv-daily/config/arxiv.json", help="Path to config JSON")
    parser.add_argument("--output", default=None, help="Override output JSON path")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    categories = config.get("categories", [])
    output_path = Path(args.output or config.get("site_data_path", "arxiv-daily/site/data/latest.json"))

    query = build_query(categories)
    papers, listing_sections = fetch_papers_by_listing(config)
    days_back = int(config.get("listing_days", 3))

    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "site_title": config.get("site_title", "arXiv Daily"),
        "generated_at": now,
        "query": query,
        "categories": categories,
        "source": config.get("source", "recent-list"),
        "listing_days": int(config.get("listing_days", days_back)),
        "days_back": days_back,
        "listing_sections": listing_sections,
        "count": len(papers),
        "papers": serialize_papers(papers),
    }

    ensure_parent(output_path)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"Wrote {len(papers)} papers to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
