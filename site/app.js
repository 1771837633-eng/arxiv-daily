const state = {
  data: null,
  category: "all",
  query: "",
  sectionsExpanded: true,
};

const els = {
  title: document.getElementById("page-title"),
  meta: document.getElementById("page-meta"),
  summary: document.getElementById("summary"),
  filters: document.getElementById("filters"),
  list: document.getElementById("paper-list"),
  search: document.getElementById("search"),
  refresh: document.getElementById("refresh"),
};

const CATEGORY_LABELS_ZH = {
  "cond-mat.dis-nn": "无序系统与神经网络",
  "cond-mat.mes-hall": "介观系统与量子霍尔",
  "cond-mat.mtrl-sci": "材料科学",
  "cond-mat.other": "凝聚态其他",
  "cond-mat.quant-gas": "量子气体",
  "cond-mat.soft": "软凝聚态物质",
  "cond-mat.stat-mech": "统计力学",
  "cond-mat.str-el": "强关联电子系统",
  "cond-mat.supr-con": "超导电性",
};

// Map common English keywords to Chinese for display
const KEYWORD_LABELS_ZH = {
  superconductivity: "超导", superconducting: "超导", superconductor: "超导体",
  magnetic: "磁性", magnetism: "磁性", ferromagnetic: "铁磁", antiferromagnetic: "反铁磁",
  quantum: "量子", topological: "拓扑", topology: "拓扑",
  spin: "自旋", spins: "自旋", electron: "电子", electrons: "电子",
  phonon: "声子", phonons: "声子", exciton: "激子", polariton: "极化激元",
  graphene: "石墨烯", "transition metal dichalcogenide": "TMDC", mos2: "MoS₂",
  transport: "输运", thermal: "热学", optical: "光学",
  disorder: "无序", localization: "局域化",
  phase: "相变", transition: "相变", critical: "临界",
  bose: "玻色", fermi: "费米", fermions: "费米子", bosons: "玻色子",
  dft: "DFT", "density functional theory": "DFT",
  "machine learning": "机器学习", "neural network": "神经网络",
  "monte carlo": "蒙特卡洛", dmrg: "DMRG", "tensor network": "张量网络",
  hubbard: "Hubbard", heisenberg: "Heisenberg", ising: "Ising",
  moire: "莫尔条纹", "2d materials": "二维材料", "van der waals": "范德华",
  perovskite: "钙钛矿", nanowire: "纳米线", film: "薄膜", crystal: "晶体",
  insulator: "绝缘体", "mott insulator": "Mott绝缘体",
  semimetal: "半金属", weyl: "Weyl", dirac: "Dirac",
  hall: "霍尔", nematic: "向列相", "charge density wave": "电荷密度波",
  "spin liquid": "自旋液体", skyrmion: "斯格明子",
};

function labelize(tag) {
  const lower = String(tag).toLowerCase();
  if (CATEGORY_LABELS_ZH[tag]) return CATEGORY_LABELS_ZH[tag];
  if (KEYWORD_LABELS_ZH[tag]) return KEYWORD_LABELS_ZH[tag];
  if (KEYWORD_LABELS_ZH[lower]) return KEYWORD_LABELS_ZH[lower];
  // Try partial match
  for (const [en, zh] of Object.entries(KEYWORD_LABELS_ZH)) {
    if (lower.includes(en) || en.includes(lower)) return zh;
  }
  // Clean up common arXiv prefixes
  return tag.replace(/^cond-mat\./, "").replace(/^physics\./, "").replace(/-/g, " ");
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
  }).format(date);
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function uniqCategories(papers) {
  const seen = new Set();
  const result = [];
  for (const paper of papers) {
    for (const category of paper.categories || []) {
      if (category.startsWith("cond-mat.") && !seen.has(category)) {
        seen.add(category);
        result.push(category);
      }
    }
  }
  return result;
}

function filteredPapers() {
  const papers = state.data?.papers || [];
  return papers.filter((paper) => {
    const categoryOk = state.category === "all" || (paper.categories || []).includes(state.category);
    if (!state.query) return categoryOk;
    const haystack = [
      paper.title,
      paper.abstract,
      paper.study_overview_zh,
      paper.abstract_summary_zh,
      paper.main_content_zh,
      paper.method_zh,
      (paper.authors || []).join(" "),
      (paper.keywords || []).join(" "),
    ].join(" ").toLowerCase();
    const q = state.query.toLowerCase();
    // Support multi-word search
    const terms = q.split(/\s+/).filter(Boolean);
    const queryOk = terms.every(function(t) { return haystack.includes(t); });
    return categoryOk && queryOk;
  });
}

function renderMetrics(papers) {
  const categories = uniqCategories(papers);
  const llmCount = papers.filter(function(p) {
    return (p.summary_mode || "").startsWith("llm-");
  }).length;
  els.summary.innerHTML =
    '<div class="metric"><div class="label">凝聚态论文</div><div class="value">' + papers.length + '</div></div>' +
    '<div class="metric"><div class="label">AI 总结</div><div class="value">' + llmCount + '</div></div>' +
    '<div class="metric"><div class="label">更新时间</div><div class="value">' + formatDate(state.data.generated_at) + '</div></div>';
}

function renderFilters(papers) {
  const categories = uniqCategories(papers);
  var html = '<button class="chip' + (state.category === "all" ? " active" : "") + '" data-category="all">全部</button>';
  for (var i = 0; i < categories.length; i++) {
    var cat = categories[i];
    var active = state.category === cat ? " active" : "";
    html += '<button class="chip' + active + '" data-category="' + escapeHtml(cat) + '" title="' + escapeHtml(cat) + '">' + escapeHtml(labelize(cat)) + '</button>';
  }
  els.filters.innerHTML = html;
}

function renderSummaryField(label, value, highlight) {
  if (!value) return "";
  var cls = highlight ? "summary-block highlight" : "summary-block";
  return '<div class="' + cls + '"><div class="summary-label">' + label + '</div><div class="summary-text">' + escapeHtml(value) + '</div></div>';
}

function renderPapers(papers) {
  if (!papers.length) {
    els.list.innerHTML = '<div class="paper"><div class="muted">没有匹配的论文。</div></div>';
    return;
  }

  // Group papers by publishing date section
  var sections = state.data.listing_sections || [];
  var sectionMap = {};
  if (sections.length) {
    for (var si = 0; si < sections.length; si++) {
      var ids = sections[si].ids || [];
      for (var ii = 0; ii < ids.length; ii++) {
        sectionMap[ids[ii]] = si;
      }
    }
  }

  var currentSection = -1;
  var html = "";

  for (var i = 0; i < papers.length; i++) {
    var paper = papers[i];
    var sectionIdx = sectionMap[paper.arxiv_id];
    if (sectionIdx !== undefined && sectionIdx !== currentSection) {
      currentSection = sectionIdx;
      var sectionTitle = sections[currentSection].title || "";
      // Collapse repeated "showing X of X entries" for cleaner display
      sectionTitle = sectionTitle.replace(/\s*\(showing \d+ of \d+ entries\s*\)/i, "");
      html += '<div class="section-header">' + escapeHtml(sectionTitle) + '</div>';
    }

    var authors = escapeHtml((paper.authors || []).slice(0, 4).join(", "));
    var moreAuthors = (paper.authors || []).length > 4 ? " 等" : "";
    var title = escapeHtml(paper.title);
    var arxivId = escapeHtml(paper.arxiv_id);
    var absUrl = encodeURI(paper.abs_url || "#");
    var pdfUrl = encodeURI(paper.pdf_url || "#");
    var mode = paper.summary_mode || "";
    var isLLM = mode.startsWith("llm-");
    var modeLabel = isLLM ? "🤖 AI 总结" : "📋 规则总结";
    var modeClass = isLLM ? "badge-llm" : "badge-rule";

    // Tags from category and keywords
    var tagsHtml = "";
    if (paper.primary_category) {
      tagsHtml += '<span class="tag tag-cat">' + escapeHtml(labelize(paper.primary_category)) + '</span>';
    }
    var kw = (paper.keywords || []).slice(0, 5);
    for (var k = 0; k < kw.length; k++) {
      tagsHtml += '<span class="tag">' + escapeHtml(labelize(kw[k])) + '</span>';
    }

    html += '<article class="paper">' +
      '<div class="paper-head">' +
        '<div>' +
          '<h2 class="paper-title"><a href="' + absUrl + '" target="_blank" rel="noreferrer">' + title + '</a></h2>' +
          '<div class="paper-meta">' +
            '<span>' + authors + moreAuthors + '</span>' +
            '<span>·</span>' +
            '<span>' + formatDate(paper.published) + '</span>' +
            '<span>·</span>' +
            '<span class="' + modeClass + '">' + modeLabel + '</span>' +
          '</div>' +
        '</div>' +
        '<div class="paper-id">' + arxivId + '</div>' +
      '</div>' +
      '<div class="paper-summary">' +
        renderSummaryField("📌 研究概览", paper.study_overview_zh, isLLM) +
        renderSummaryField("📝 摘要概括", paper.abstract_summary_zh) +
        renderSummaryField("📄 主要内容", paper.main_content_zh) +
        renderSummaryField("🔬 方法", paper.method_zh) +
      '</div>' +
      '<div class="tags">' + tagsHtml + '</div>' +
      '<div class="paper-actions">' +
        '<a href="' + absUrl + '" target="_blank" rel="noreferrer">arXiv Abstract</a>' +
        '<a href="' + pdfUrl + '" target="_blank" rel="noreferrer">PDF</a>' +
      '</div>' +
    '</article>';
  }

  els.list.innerHTML = html;
}

function typesetMath() {
  if (window.MathJax && typeof window.MathJax.typesetPromise === "function") {
    window.MathJax.typesetPromise([els.list]).catch(function() {});
  }
}

function rerender() {
  var papers = filteredPapers();
  renderMetrics(state.data.papers || []);
  renderFilters(state.data.papers || []);
  renderPapers(papers);
  els.meta.textContent = "显示 " + papers.length + " / " + (state.data.papers || []).length + " 篇论文";
  typesetMath();
}

async function loadData() {
  var response = await fetch("./data/latest.json", { cache: "no-store" });
  if (!response.ok) throw new Error("加载失败: " + response.status);
  state.data = await response.json();
  els.title.textContent = state.data.site_title || "arXiv Daily";
  state.category = "all";
  rerender();
}

els.search.addEventListener("input", function(event) {
  state.query = event.target.value.trim();
  rerender();
});

els.refresh.addEventListener("click", async function() {
  els.refresh.disabled = true;
  try { await loadData(); }
  finally { els.refresh.disabled = false; }
});

els.filters.addEventListener("click", function(event) {
  var button = event.target.closest("[data-category]");
  if (!button) return;
  state.category = button.dataset.category;
  rerender();
});

loadData().catch(function(error) {
  els.meta.textContent = "加载失败：" + error.message;
  els.list.innerHTML = '<div class="paper"><div class="muted">请先运行抓取脚本: python scripts/fetch_arxiv.py</div></div>';
});
