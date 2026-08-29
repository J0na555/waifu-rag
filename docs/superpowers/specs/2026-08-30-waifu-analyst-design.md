# Waifu Analyst Design

Date: 2026-08-30
Status: validated design, awaiting user review
Refs: bot-plan.md (seed list), dossier-template-v0.1.md (schema v0.2),
dossiers/taiga-aisaka.md (verified tier A pilot)

## Product

An Oracle-style RAG chatbot with an Analyst voice. It explains waifus, it does
not just identify them. The bot answers "why do people love her" questions,
trope-subtype questions, comparisons across characters, taste-grounded
recommendations, character-grounded what-ifs, and narrative role analysis.
Every opinion is traceable to evidence: polls, fan discourse, sourced quotes.

Not the Vessel (no in-character roleplay). Not a fact-lookup bot (a worse
Wikipedia). Not a Matchmaker-first product, though matching logic is a natural
later layer over the same data.

## Question classes the bot must answer

1. Why-questions: "Why do people actually love Taiga?" Appeal decomposition.
2. Trope-subtype questions: "Who's a tsundere whose cold side is genuine
   bitterness, not slapstick?"
3. Comparisons: "Taiga vs Asuka, as characters." Needs personality, narrative,
   and community fields from both dossiers.
4. Taste-grounded recommendation: "I like melancholic, quietly competent types.
   Who else?" Built on archetype tags plus appeal fields.
5. Character-grounded what-ifs: "What would she think of this situation?"
   Analysis against subtype_nuance and speech_style, not roleplay.
6. Narrative role analysis: "What does she do to the story, structurally?"

## Data model: the dossier

One markdown file per character, fields as retrieval units. Schema v0.2:

- identity, base
- personality: archetype, subtype_nuance, strengths, flaws, mannerisms,
  speech_style, voice_performance (optional)
- narrative: structural_role, arc, key_moments
- relationships
- appeal: design, personality, narrative, cultural
- reception_data, community, verified_quotes, adaptation_notes (optional),
  sources, tier + confidence

Load-bearing fields: subtype_nuance, appeal, community. These carry the Analyst
voice. Everything else is muscle.

Global rules: source contract (every non-common-sense claim tagged), no invented
opinions (debate not verdict), no unbacked superlatives, length budgets, tier
gate rule 7 (a dossier cannot be tier A until verified_quotes, reception_data,
and confirmed key_moments exist).

Tiers: A (human-reviewed, gate passed), B (LLM-drafted, unreviewed), C (raw
wiki notes only). The bot answers honestly from every tier and says which tier
an answer came from.

## Seed set

39 characters across 13 archetype buckets, one to three per bucket, listed in
bot-plan.md. Buckets: tsundere, kuudere, dandere, genki, deredere, yandere
(+ predator subtype), ojou-sama, onee-san/caretaker, cool beauty/warrior,
chuunibyou, idol/performer, two-faced/hidden identity, otherworldly.

Deliberately not the raw top 50 by popularity: popularity skews toward a few
archetypes, and archetype variety is the real sample size for the template.

Tier plan for the first build: A-tier for the bucket representatives plus
cross-bucket characters (roughly 12 to 15), B-tier for the rest of the 39.

## Sources (three layers)

- Skeleton: AniList GraphQL API, structured metadata and personality tags.
- Flesh: Fandom wikis and TV Tropes, MediaWiki APIs, personality and plot
  sections, trope nuance.
- Soul: fan discourse, reddit threads, best girl contests, MAL reviews and
  forums, blogs. Unstructured, no clean scrape, requires reading.

Taiga verification showed the real source pattern: fandom pages (via API,
direct fetch 403s), transcript sites for quotes, MAL character page and forum
threads, saimoe archives for poll records, animeanime.jp for Japanese polls,
CBR for editorial takes.

## Pipeline

1. Seed list -> per-character source inventory script (what exists, sets the
   tier before scraping).
2. Ingestion per character from the three layers.
3. LLM drafting pass, every field tagged with source refs.
4. Verification pass per character: hunt list, quote verification, reception
   numbers, confirmed episode references, declared gaps. This is the expensive
   step, see economics below.
5. Storage: dossier md files as canonical, git-versioned.
6. Index: derived, embedded per field, never edited directly.

## Verification economics

One fully verified dossier took roughly 20-28 character-equivalents of agent
effort by extrapolation. Slow items: verbatim quotes (transcript retrieval plus
cross-checking) and tournament/poll cross-referencing. Fast items: fandom API
pages, MAL, poll pages.

Consequence: hand-verification does not scale to all 39 in the first pass.
A-tier only what the demo needs, B-tier the rest, C-tier everything else.
A booster path exists: promote B to A later, per character, when the dossier
matters.

## Architecture

Two layers plus synthesis:

- Canonical: markdown dossiers, the source of truth for humans.
- Index: derived. Every dossier field is a chunk, embedded with metadata
  (character_id, field path, archetype tags, tier). Raw sources chunked behind
  character_id for citation. Rebuildable from the dossiers at any time.
- Synthesis: the model receives retrieved field text with source tags under the
  analyst prompt.

Query: two-stage. Stage one picks candidate characters by tag filters plus
vector similarity. Stage two pulls the specific fields the question needs
within those characters. One search over shredded chunks produces mush; the
second stage is what makes answers read as analysis.

MVP note: at 39 dossiers, no vector DB server. In-process embeddings, brute
force similarity. The pattern scales; only the machinery changes.

## Product guardrails

1. Grounded opinions only: every analytical claim traces to a source field.
2. Debate not verdict: the bot presents both sides with arguments.
3. Tier transparency: answers disclose which tier the evidence came from.
4. Version awareness: claims about who a character is should know which
   adaptation they mean (Taiga's LN vs anime reading differs, and the dossier
   records it).
5. Unverified stays flagged: UNCERTAIN and DRAFT-ONLY are permanent vocabulary.

## Demo acceptance

The first build is done when:

- The pipeline produces a dossier for every seed character at its target tier.
- The bot answers the demo set from A-tier characters, citing dossier fields
  and sources: the three Taiga validation questions plus one comparison, one
  trope-subtype, one recommendation.
- No invented opinions appear in demo answers under spot check.

## Deferred and non-goals

- The Vessel (roleplay). Different data, different build, later if ever.
- Seasonal best-girl data (Anime Trending). Same pipeline, different seed
  list, later feature.
- Community meta / ranking pipeline (poll scraping). Barely uses RAG.
- Matchmaker as a product surface. Natural later layer over the same data.
- Vector DB infrastructure. Not needed at 39 dossiers.
- Fanfic reference and trivia surfaces. Same data, different wrappers.

## Open questions

1. Which A-tier characters exactly for the demo: one per bucket plus two
   cross-bucket, or a different split? User decides.
2. Transcript provenance at scale: fansub-style transcripts pass the gate today;
   does citation quality require official subs before any build ships?
3. Drafting LLM pass: which model and cost per dossier, decided at
   implementation.
4. Embedding model choice: decided at implementation, reversible.