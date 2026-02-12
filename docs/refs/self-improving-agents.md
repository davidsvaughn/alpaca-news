# Designing an LLM-Tool Trading Research System That Learns From Its Own Tool Use

## What a system like this can and cannot do

A blunt reality check: consistently extracting tradable edge from public markets is hard because competition is intense and information gets incorporated quickly. The classic “efficient markets” view argues that prices reflect available information, implying persistent outperformance is difficult without taking additional risk (and even then, outperformance is not guaranteed). citeturn22search0turn22search17

If you aim to automate trading decisions, you’re operating in a domain where many retail participants lose money. The entity["organization","Securities and Exchange Commission","us securities regulator"] has published investor education noting that day traders “typically suffer severe financial losses” early and that many never become profitable. citeturn22search2

LLMs **can** materially improve your ability to *gather, normalize, and reason over messy, unstructured inputs* (filings, news, transcripts, social chatter, macro releases) and to *run consistent research loops*—but that is not the same thing as “finding an exploitable trend.” A warning example from the research literature: a 2025 paper presented at entity["organization","NeurIPS","machine learning conference"] reports a fine-tuned agent that achieved *higher* market-state classification accuracy yet produced *lower* simulated trading returns, attributing the gap to objective mismatch (optimizing a proxy task that doesn’t align with profitability). citeturn19view0

So it helps to frame the goal precisely:

- **Feasible, high-leverage goal:** build a system that reliably turns heterogeneous information into timestamped, queryable artifacts and evaluation signals (what it fetched, why, what it concluded, what later happened). citeturn3search0turn5search0turn5search1  
- **Hard, end-to-end goal:** autonomously trade profitably and robustly across regimes, with controlled risk and without overfitting. Surveys of LLM-based trading agents emphasize that many published results are still constrained by backtesting shortcuts, data leakage risks, and fragile evaluation setups. citeturn18view0turn8search3

A practical division of labor that shows up both in industry workflows and research is: use LLMs primarily for **tool orchestration + unstructured-data understanding**, while using conventional quantitative methods for **signal construction, risk controls, and execution** (where determinism and auditability matter). citeturn22search7turn8search0

## Information sources and tool modalities worth supporting

Your tool suite (web search, social search, URL fetch, plus market/fundamental data APIs) can be thought of as producing two broad “data shapes,” and your architecture should treat them differently:

**Structured time series / records** (prices, volumes, fundamentals tables, economic series) are best stored as normalized tables with precise timestamps and corporate-action adjustments. citeturn6search1turn5search1turn5search0  
**Unstructured text streams** (articles, filings text, posts) are best stored as immutable documents + extracted annotations (entities, events, sentiment, claims), with origin metadata and retrieval scores. citeturn5search0turn7search6turn22search7

Below are the highest-signal categories (not “alpha,” but high-signal *inputs*) that systems like yours usually ingest.

Market data (quotes/trades/bars)
Real-time and historical market data APIs give you OHLCV bars, sometimes ticks, and sometimes WebSocket streaming. Examples include entity["company","Alpha Vantage","market data api provider"] (intraday OHLCV), entity["company","Tiingo","market data provider"] (IEX-based real-time endpoints), and entity["company","Polygon.io","market data api provider"] (WebSocket streams for trades/aggregates). citeturn6search1turn6search2turn6search8turn6search0  

If you care about *true* real-time behavior, pay attention to exchange licensing, feed type (SIP vs proprietary), and whether you’re consuming a broker’s consolidated feed vs a single venue. Even venue-specific feeds are described as proprietary “real-time feeds” by the entity["organization","IEX Exchange","us stock exchange"]. citeturn5search2

Regulatory filings and fundamentals
For U.S. issuers, filings are one of the cleanest “truth sources” because they’re primary disclosures. The SEC’s entity["organization","EDGAR","sec electronic filing system"] APIs provide JSON access to submission history and XBRL financial statement data (including forms such as 10-K, 10-Q, and 8-K), and the SEC notes these JSON structures are updated throughout the day as submissions come in and do not require API keys. citeturn5search0turn5search12  

This kind of source is ideal for an “exploration & self-review” loop because:
- the documents are time-stamped and immutable (good for reproducibility),
- extraction quality is measurable (did you detect the right event?),
- and it’s harder to manipulate than social content. citeturn5search0turn8search2

Macro and rates data
For macro context and regime features, entity["organization","Federal Reserve Bank of St. Louis","st louis, mo, us"] provides the entity["organization","FRED","economic data api"] API for economic time series and related metadata. citeturn5search1turn5search22  

Macro data is where timestamps, revisions, and “vintage” handling matter if you want realistic backtests (what was known when). Even third-party wrappers emphasize point-in-time handling via ALFRED vintages. citeturn5search19

News and global events
If you want a robust alternative to scraping random sites, the entity["organization","GDELT Project","global news event database"] offers a large-scale event and news-derived dataset described as a near-real-time “global graph” of society via news media, with extensive documentation and bulk download options. citeturn7search6turn7search3  

This is attractive for systematic experimentation because you can standardize queries and compare extraction methods over a consistent corpus. citeturn7search3turn7search9

Social / crowd content (treat as adversarial)
If you ingest social content for “sentiment” or narrative detection, treat it as *hostile terrain*. The SEC explicitly warns that fraudsters can manipulate share prices by spreading rumors on social media and profit at investors’ expense. citeturn8search6turn8search2  

The platform APIs also impose practical constraints. The entity["company","X","social media platform"] API’s “recent search” endpoint is described as retrieving posts from the last 7 days, and the platform documents rate limits as central constraints on how many requests you can make per time window. citeturn7search4turn7search2  

This matters for your “learning to use tools better” goal: the optimal strategy may be *not* to over-invest in a source whose coverage window and rate limits force you into sampling artifacts rather than true coverage. citeturn7search4turn7search8

## Architecture choices: agentic loop vs deterministic pipelines

A useful way to think about “agents” is: they are tool-using loops that can decide *what to do next*. A “pipeline” is a mostly predetermined graph of steps that runs in a fixed order. citeturn4search15

You do not need a fully general agent to get the “explore → log → review → improve” behavior you described. But you **do** need (a) multi-step tool calling, (b) state, and (c) evaluation signals. Those can be implemented either as:
- a **workflow orchestrator** (fixed DAG with occasional branches), or
- an **agentic controller** (dynamic planning, repeated tool calls), or
- a hybrid (which is what most production systems converge on). citeturn4search15turn0search4

Tool calling as the backbone
At the API level, modern “tool calling” is typically implemented as a multi-step exchange: you provide tool schemas, the model emits a tool call with arguments, your code executes it, then you feed the tool output back for the model to incorporate. citeturn15view0  

This pattern is explicitly described (including vocabulary like “tools,” “tool calls,” tool outputs, and a multi-step flow) in entity["organization","OpenAI","ai company"]’s function/tool calling documentation. citeturn15view0

For orchestrating many steps and retaining full provenance, a first-class tracing system matters more than whether you label something “agent” or “pipeline.” The entity["organization","OpenAI","ai company"] entity["organization","OpenAI Agents SDK","openai agent framework"] explicitly positions agentic apps as tool-using systems with handoffs and “a full trace of what happened.” citeturn14view0

Agent-like reasoning patterns that work well for exploration
A dominant pattern for tool-using reasoning is “think → act → observe → think…”, popularized by ReAct (“Reasoning and Acting”), which combines intermediate reasoning traces with tool actions. citeturn0search4turn0search0  

For your use-case, the big win of this pattern is not philosophical. It’s operational: you can log each action/observation pair and later evaluate whether a tool call was useful, redundant, or misleading. citeturn3search0turn3search13

Tool discovery and “acquiring new tools”
Your idea of “learning what tools are worth it” becomes more scalable if you formalize tools behind a standard interface that supports discovery (list tools, get schemas, call tools). The Model Context Protocol (MCP)—introduced publicly by entity["company","Anthropic","ai company"] as an open standard for connecting models to external data/tools—formalizes servers exposing tools with names and schemas. citeturn20search10turn20search4  

On the OpenAI side, OpenAI’s connectors/MCP documentation describes a concrete discovery step: when you specify a remote MCP server, the API attempts to list tools from the server and returns an output item containing imported tools. citeturn20search1  

This kind of mechanism is how “tool acquisition” can be made real: your system can enumerate tools, store their schemas/docs, and then (crucially) evaluate tool utility over time using the same logging and scoring machinery you apply to web and market data. citeturn20search1turn16view0

Where agents help, and where they hurt
For trading research, agents are most useful in these roles:
- **Hypothesis generation and refresh** (what changed? what to test next?),
- **Contextual retrieval plans** (what filings/news/macro series should be fetched given a question),
- **Data QA and reconciliation** (why do two sources disagree?),
- **Postmortems** (why did this run waste tokens/time; what should be pruned?). citeturn18view0turn3search13  

Agents are most dangerous when you let them:
- directly execute trades without hard limits,
- browse untrusted content with write-capable tools,
- or “optimize” on short-horizon backtests without strong leakage controls. citeturn8search3turn21search0turn8search0

## Traceability and storage: building a real “flight recorder”

Your logging requirement (timestamped data + full traces showing which tool calls produced it) is not optional if you want sustainable improvement. Without a high-fidelity record, “self-review” degenerates into vibes.

Distributed tracing standards map well to LLM systems
A standard way to represent multi-step execution is a **trace** made of **spans** (each span is one unit of work, with timestamps and attributes). entity["organization","OpenTelemetry","observability standard"] documents traces/spans and how exporters send traces to backends (console, collectors, vendor systems). citeturn3search0turn3search8  

This structure matches your needs because you can model:
- one top-level trace per “research run,”
- child spans for each model call,
- child spans for each tool call (web query, X search query, filings fetch, price history call),
- plus derived spans for parsing, feature extraction, and evaluation. citeturn3search0turn3search9

LLM-focused observability backends already speak “trace”
Several modern LLM observability products explicitly structure runs as traces/spans and ingest via OpenTelemetry protocols:

- entity["organization","Arize Phoenix","llm observability tool"] describes tracing as capturing a run step-by-step (model calls, retrieval, tool use, custom logic) and notes OTLP ingestion and auto-instrumentation for common frameworks. citeturn3search13turn3search2  
- entity["organization","Langfuse","llm observability platform"] describes capturing traces with nested observations including timing, inputs, outputs, and cost. citeturn11search0turn11search4  
- OpenAI’s cookbook example on evaluating agents with entity["organization","Langfuse","llm observability platform"] describes traces containing spans for agent runs, tool calls, and model calls. citeturn11search17turn9search3  
- entity["organization","LangSmith","llm tracing platform"] positions itself as tracing + evaluation, including online evaluation of real user interactions. citeturn3search20turn3search1  

You can choose any backend; the important part is: **store your raw artifacts separately from your trace metadata** so you can re-run parsing and feature extraction later without re-fetching sources (and so your provenance stays intact). citeturn5search0turn3search0

image_group{"layout":"carousel","aspect_ratio":"16:9","query":["Langfuse trace UI screenshot nested spans","Arize Phoenix LLM tracing UI screenshot","LangSmith tracing UI screenshot","OpenTelemetry trace spans diagram"],"num_per_query":1}

A concrete storage pattern that stays sane
A sustainable design is an **append-only event log** (durable, immutable) plus derived “materialized views” (fast query tables). The immutable event log is the only thing you truly trust; everything else can be recomputed.

At minimum, store these event types:

- **ToolCallRequested**: tool name, normalized arguments, parent span id  
- **ToolCallReturned**: raw output blob (or pointer), latency, bytes, parse status  
- **DocIngested**: canonical URL/id, fetch timestamp, content hash  
- **ExtractionProduced**: extracted entities/events/signals + confidence + versioned extractor id  
- **DecisionProposed**: (if you do trading) proposed trade, rationale, constraints, risk checks  
- **DecisionExecuted / Rejected**: execution outcome and why  
- **OutcomeObserved**: later realized return, drawdown contribution, slippage, etc. citeturn3search0turn8search0  

Here’s a minimal JSON-style schema for a single event (illustrative, not a standard):

```json
{
  "event_id": "uuid",
  "run_id": "uuid",
  "trace_id": "otel_trace_id",
  "span_id": "otel_span_id",
  "parent_span_id": "otel_parent_span_id",
  "ts_utc": "2026-02-11T15:04:05.123Z",
  "event_type": "tool_call_returned",
  "tool_name": "edgar_submissions",
  "tool_args": {"cik": "0000320193"},
  "artifact_ref": "s3://bucket/raw/....json",
  "content_hash": "sha256:...",
  "latency_ms": 842,
  "token_cost": {"prompt": 0, "completion": 0},
  "status": "ok",
  "error": null,
  "tags": {"asset": "AAPL", "task": "filings_refresh"}
}
```

If you adopt OpenTelemetry IDs, you get compatibility with a large ecosystem of tracing and correlation tools (and you can still keep your own domain IDs and domain-specific tables). citeturn3search0turn3search4

## What “lessons learned” look like in modern agent research

A key point: most “self-improving agent” work is **not** about the model updating its weights online. It’s about the system updating *text, memory, retrieval indices, prompts, or tool-use policies* between episodes. Multiple well-cited lines of research converge on this.

Reflection as stored text that changes future behavior
The “Reflexion” framework explicitly proposes improving a language agent not by weight updates but by producing **verbal reflections** from feedback signals and storing them in an **episodic memory buffer** to guide later trials. citeturn0search2turn0search6  

This is directly aligned with your goal: “lessons” can be stored as structured natural language objects (plus metadata), and later retrieved when similar situations arise.

“Experience logs → higher-level reflections”
In “Generative Agents,” the proposed architecture stores a complete record of an agent’s experiences in natural language, synthesizes them into higher-level reflections, and retrieves them to plan behavior. citeturn1search0turn1search11  

Even though that work is about simulated people, the lesson-transfer is straightforward: your trading research system can store raw “episodes,” then periodically distill them into more compact “what worked / what didn’t” summaries keyed to contexts (market regime, tool type, asset class, task). citeturn1search0turn5search0

Procedural knowledge as a “skill library”
“Voyager” (lifelong learning in Minecraft) is notable because it frames learned behavior as an explicit **skill library** that accumulates reusable procedures the agent can call for new tasks. citeturn0search3turn0search7  

In a trading research context, “skills” often end up being:
- parameterized query templates (“fetch latest 8-Ks for ticker X; extract items Y”),
- parsing/extraction code modules,
- or multi-step retrieval recipes (“macro shock check: pull CPI release; compare expectations; map sector sensitivity”). citeturn5search0turn5search1turn5search12  

This is where “skills.md” (or similar) can work—**but you should treat it as a compiled artifact**, not a dumping ground. If it grows without structure, it becomes another prompt soup.

Long-term memory as a managed resource, not a growing context window
“MemGPT” frames memory as a hierarchy of tiers and uses mechanisms akin to paging/interrupts to manage limited context. citeturn1search1turn1search5  

This is a direct answer to your “don’t become bloated” constraint: the sustainable form of “learned lessons” is not “keep everything in the prompt,” but rather “keep everything in storage, and learn retrieval + distillation policies.” citeturn1search1turn3search0

Tool-use competence can be trained, benchmarked, and stabilized
Several research threads focus specifically on models learning to call tools:

- “Toolformer” proposes that LMs can teach themselves to use tools via self-supervision, learning when to call APIs and how to integrate outputs. citeturn0search1turn0search5  
- “ToolLLM” and related work introduce large-scale datasets/benchmarks for tool use (ToolBench) and discuss framework elements for training and evaluation. citeturn1search3turn1search10  
- “API-Bank” provides an executable evaluation system and training data for tool-augmented dialogues, explicitly treating planning/retrieval/tool calling as measurable competencies. citeturn9search1turn9search5  
- “ToolBench” positions itself as an open platform for training/serving/evaluating tool learning, and “StableToolBench” discusses stabilizing evaluation with caching and simulators because APIs change over time—this is extremely relevant to your “save all data with traces” requirement. citeturn1search2turn1search13  
- “Gorilla” argues that retrieval over API documentation + training can reduce hallucinated tool usage and adapt to changing docs (retrieval-aware training). citeturn9search0turn9search8turn20search3  

Put differently: “learning to use tools better” is not only possible—it’s one of the most actively explored directions in agent research—but the best-performing systems typically combine **(a) retrieval over tool docs/schemas, (b) supervised or synthetic training data, and (c) rigorous tool-call evaluation harnesses**. citeturn9search0turn1search10turn1search13

## Improving over time without turning into a mess

If you want sustained improvement, you need two things that many agent demos lack: (1) a stable target behavior you can measure, and (2) a controlled mechanism for updating the system based on those measurements. There are several proven mechanisms, each with tradeoffs.

Iterative self-critique loops (no weight updates)
“Self-Refine” shows an iterative loop where the model produces an output, critiques it, and refines it repeatedly—without additional training data or reinforcement learning. citeturn2search2turn2search6  

In your context, Self-Refine-like loops are most valuable for:
- improving query plans (“what should I fetch next?”),
- improving extraction instructions (“how to parse this filing?”),
- and improving report quality (“what evidence supports this claim?”). citeturn5search0turn2search2  

They are **not** a substitute for quantitative evaluation of trading outcomes.

Prompt and instruction optimization (treat prompts as parameters)
OPRO (“LLMs as optimizers”) frames optimization as repeatedly proposing new candidate instructions/prompts conditioned on prior candidates and their scores—explicitly applying the idea to prompt optimization. citeturn4search0turn4search6  

Promptbreeder similarly treats prompts as evolving artifacts, using an evolutionary process that mutates and selects prompts (including “mutation prompts” that guide how mutations happen). citeturn4search1turn4search4  

These methods are powerful if—and only if—you have a robust evaluation set and you can prevent reward hacking (for trading, that means careful out-of-sample and forward testing, not just optimizing on a backtest). citeturn8search3turn22search3

Declarative “self-improving pipelines” instead of ad-hoc prompts
entity["organization","DSPy","llm programming framework"] explicitly positions itself as “declarative self-improving” Python modules, aiming to make LLM programs more reliable and maintainable than hand-managed prompting. citeturn4search12  

This approach can map well to your use-case: you can express retrieval, extraction, and synthesis as modules, then use optimization/search methods to tune module instructions based on logged performance. citeturn4search12turn3search16

Evaluation-driven development for agents
To prevent “learning” from degenerating into story-telling, you need evaluators that can run automatically on logged traces.

- entity["organization","TruLens","llm evaluation framework"] describes “feedback functions” as a programmatic way to generate evaluations on an application run (often using another model as an evaluator). citeturn3search3turn3search7  
- entity["organization","Weights & Biases","ml ops company"]’s Weave docs describe evaluation-driven LLM development centered on an `Evaluation` object that measures behavior over curated examples. citeturn11search2turn11search20  
- entity["organization","LangSmith","llm tracing platform"] similarly emphasizes tracing + evaluation, including online evaluation on real traffic. citeturn3search1turn3search20  

You can apply this by defining multiple “scores,” not only PnL:
- tool usefulness (did this call add novel information?),
- source credibility (is this primary vs secondary; is it known-scammy),
- extraction correctness (did it correctly classify/locate the relevant filing section),
- latency and cost,
- and only downstream: predictive value / trading utility. citeturn3search3turn5search0turn8search6

Memory compaction and “forgetting” as a first-class policy
The highest-quality way to avoid bloat is to treat memory as:
1) immutable raw logs, and  
2) a compact, actively maintained “working set” derived from those logs.  

“MemGPT” is essentially a formalization of this idea: manage multiple memory tiers so the model only sees what it needs, when it needs it. citeturn1search1turn1search5  

In practice, this means your “lessons” store should support:
- versioning (lessons can be superseded),
- confidence and decay (lessons can expire),
- and retrieval gating (only inject lessons that match the current task/regime). citeturn1search1turn3search0

## Evaluation, safety, and the non-negotiable “reality checks” for markets

Backtests are where most “self-improving trading systems” fool themselves. Your loop will tend to optimize whatever feedback you make easy to measure—so make sure the feedback is not a trap.

Backtest overfitting is common and quantifiable
Bailey and López de Prado’s work on the “probability of backtest overfitting” proposes a framework (including combinatorially symmetric cross-validation) to estimate how likely a chosen strategy is overfit in the context of investment simulations. citeturn8search7turn8search3  

The entity["organization","CFA Institute","investment professional association"] also highlights overfitting as a major backtesting risk: a strategy can look exceptional on historical data and fail in new regimes. citeturn22search3  

If you introduce an LLM into the loop, you add new overfitting surfaces:
- it can meta-optimize toward quirks of your evaluation set,
- it can exploit leakage in your retrieval process,
- and it can find shortcuts that improve a proxy score but not real outcomes (exactly the failure mode described in the NeurIPS 2025 “predicts but loses” case). citeturn19view0turn22search3

Algorithmic trading requires controls, even in “research” mode
Even if you are not a broker-dealer, it’s smart to borrow the control philosophy from regulated environments.

entity["organization","FINRA","us broker-dealer regulator"] summarizes the SEC’s Market Access Rule (Exchange Act Rule 15c3-5) as requiring firms with market access to control risks associated with market access to protect market integrity and stability. citeturn8search0  
The rule itself (as codified) describes obligations around risk management controls and supervisory procedures designed to manage financial/regulatory risks of market access. citeturn8search8  
SEC staff FAQs also discuss pre-order risk management controls under this rule. citeturn8search4  

Translated into your system design: if the system ever progresses from “research” to “execution,” you want hard guardrails that an LLM cannot talk its way around: max order sizes, kill switches, allowed instruments, and independent risk checks. citeturn8search0turn8search4

Social data is manipulable and sometimes criminal
For any strategy that “spots trends” by watching social chatter, you have to confront manipulation directly. The SEC has published investor alerts describing rumor-based manipulation on social platforms. citeturn8search6turn8search2  
The SEC has also brought enforcement actions involving social-media-driven stock manipulation promoted on platforms like Twitter and Discord. citeturn8search10  

This has a direct implication for your exploration loop: some of your “best performing” information sources may be best at producing *compelling narratives*, not reliable signals. Your reviewer model (or self-review stage) needs explicit checks for source credibility and manipulation indicators. citeturn8search6turn3search7

Tool-using agents have a security problem: prompt injection
Once you combine web browsing + tool calls (especially write-capable tools), you inherit the prompt-injection threat class. entity["organization","OWASP","application security nonprofit"]’s prompt injection prevention cheat sheet describes prompt injection as a vulnerability where malicious input manipulates model behavior, in part because instructions and data are processed together. citeturn21search0turn21search3  

If you also adopt MCP-style tool ecosystems, security guidance becomes even more important. MCP documentation includes explicit security best practices discussing risks like “expanded blast radius” from overly broad tokens/scopes. citeturn21search2turn20search4  

For a trading-adjacent system, the sober takeaway is: **never let a model directly execute privileged actions based on untrusted text** (webpages, posts, emails). Separate:
- browse/fetch (untrusted),
- parse/extract (sanitized),
- decide (policy-checked),
- execute (guardrailed). citeturn21search0turn8search0

A final “tell it like it is” summary
What you are proposing is realistic if you define “learning” as **system-level improvement**: better retrieval recipes, better tool selection, better parsing/extraction, better evaluation discipline, and better filtering of bad sources. Research frameworks like Reflexion, Generative Agents, Voyager, and MemGPT all embody this idea in different forms (reflections, experience logs, skill libraries, hierarchical memory). citeturn0search2turn1search0turn0search3turn1search1  

But if the hidden goal is “let the model browse the web and discover a consistently exploitable market pattern,” you should expect the dominant failure mode to be **overfitting + narrative seduction** unless evaluation is brutal, leakage-resistant, and grounded in realistic forward testing and risk controls. The published case where an agent predicts better but earns less is not an edge case; it’s the warning label. citeturn19view0turn8search3turn22search3