# Experiential Glocalization

## Phase 1: Experiential Learning and Memory Evolution

### Stage 1: Input

The framework receives two versions of content: English source text and glocalized Arabic content.

---

### Stage 2: Seed Memory Construction (`build-seed-memory` $\to$ Release $M_0$)

* **Input**:
  * The **Seed** subset of English source and glocalized Arabic pairs.
* **Process**:
  1. **Pair Analysis (Agent A1 - Pair Analyst)**:
     * Aligns English phrases and cultural elements with their counterparts in the glocalized Arabic reference.
     * Categorizes each strategic decision into:
       * **`retain`**: Preserving factual, narrative, or domain-critical terms.
       * **`substitute`**: Replacing culture-bound or incidental foreign elements with culturally resonant Arab counterparts (e.g., replacing *"the American Dream"* with *`قصة نجاح ملهمة`* [an inspiring success story]).
  2. **Reference Quality Validation (Agent A5 - Quality Evaluator)**:
     * Audits the seed pairs to verify that the glocalized reference meets high quality standards (meaning preservation, cultural resonance, and natural Modern Standard Arabic syntax).
  3. **Vector Indexing**:
     * Admitted pairs are stored as immutable `reference_example` experiences.
     * A dense vector index (e.g., 512-dimensional `hashing-unibigram-v1`) is computed across all source profiles to enable semantic retrieval.
* **Output**:
  * **Memory Release $M_0$**: A base seed repository containing validated episodic demonstration cases ready to guide future adaptations.

---

### Stage 3: Experiential Practice & Reflexive Self-Correction (`collect-practice`)

* **Input**:
  * English source texts.
  * The glocalized Arabic reference.
* **Process**:
  The system engages in an experiential trial-and-error loop:
  1. **Source Profiling & Planning (Agent A3 - Planner)**:
     * Analyzes the source text to detect cultural entities and domain constraints.
     * Queries $M_0$ memory for relevant demonstrations.
     * Generates a structured `PlanPayload` declaring exact `retain` and `substitute` decisions with linguistic rationales.
  2. **Content Generation (Agent A4 - Generator)**:
     * Synthesizes natural Modern Standard Arabic fulfilling all plan decisions, tracking exact text quote locators.
  3. **Quality Evaluation (Agent A5 - Evaluator)**:
     * Compares the candidate against the gold glocalized reference. It recognizes that replacing Western cultural traits, places, dishes, and idioms with Arab counterparts is intentional and valid glocalization.
  4. **Diagnostic Reflection (Agent A6 - Reflector)**:
     * If an attempt fails (e.g., due to an awkward literal calque like translating *"healthy debate"* as *`نقاش صحي`*), Agent A6 inspects the failure alongside hints from the gold glocalized Arabic reference.
     * It outputs actionable diagnostic feedback (e.g., *`CALQUE_DETECTED: Localize into Arab communicative values emphasizing calm, constructive discourse and mutual respect`*), triggering a self-correcting re-planning cycle.
* **Output**:
  * Trajectory records documenting both failed attempts (negative episodes) and successful self-corrected attempts (positive episodes).

---

### Stage 4: Insights Constructions (`update-insights` $\to$ Release $M_1$)

* **Input**:
  * Trajectory execution histories from the practice trials (successes, failures, and self-corrections) alongside seed demonstrations.
  * **Example Practice Trajectory History (`Pair 000020`)**:
    * **Source Text**: *"They engage in healthy debate to reach optimal solutions."*
    * **Attempt 1 (Negative Episode — `exp_att_000020_1`, Outcome: `failure`)**:
      * *Plan Decision*: `retain` literal phrasing (*"healthy debate"* → *`نقاش صحي`*, *"optimal solutions"* → *`حلول مثالية`*).
      * *Generated Output*: *"يشاركون في نقاش صحي للوصول إلى حلول مثالية."*
      * *Evaluator & Reflector Feedback*: **FAIL** (`cultural_fit`: 2/4, `arabic_quality`: 2/4) — `CALQUE_DETECTED`: *"نقاش صحي"* is an awkward medical/hygiene calque in Arabic; localize into Arab deliberative etiquette (*أدب الحوار*).
    * **Attempt 2 (Positive Episode — `exp_att_000020_2`, Outcome: `success`)**:
      * *Self-Corrected Plan Decision*: `substitute` with culturally resonant phrasing (*"healthy debate"* → *`نقاش هادئ وبنّاء يتسم بالاحترام`*, *"optimal solutions"* → *`أفضل الحلول`*).
      * *Generated Output*: *"يشاركون في نقاش هادئ وبنّاء يتسم بالاحترام للوصول إلى أفضل الحلول."*
      * *Evaluator Verdict*: **PASS** (`meaning_and_purpose`: 4/4, `cultural_fit`: 4/4, `arabic_quality`: 4/4, `plan_compliance`: 4/4).
* **Process**:
  1. **Comparison Packet Construction**:
     * Pairs negative episodes with their self-corrected positive counterparts (`failure_success` packets) to highlight why a specific localization strategy succeeded where literal translation failed.
     * **Example Constructed Comparison Packet (`Pair 000020`)**:
       * **Packet Metadata**: `comparison_id: "cmp_failure_success_000020"`, `kind: "failure_success"`, `domain: "general"`.
       * **Record A (Failed Record — `exp_att_000020_1`)**:
         * `outcome`: `"failure"` | `strategy`: `retain` (*"healthy debate"* → *`نقاش صحي`*, *"optimal solutions"* → *`حلول مثالية`*)
         * `arabic_excerpt`: *"يشاركون في نقاش صحي للوصول إلى حلول مثالية."*
         * `assessment`: Rejected due to clinical English calque (`cultural_fit: 2/4`, `arabic_quality: 2/4`).
       * **Record B (Self-Corrected Record — `exp_att_000020_2`)**:
         * `outcome`: `"success"` | `strategy`: `substitute` (*"healthy debate"* → *`نقاش هادئ وبنّاء يتسم بالاحترام`*, *"optimal solutions"* → *`أفضل الحلول`*)
         * `reflection_received`: *"Localize 'healthy debate' into Arab communicative values emphasizing calm, constructive discourse and mutual respect."*
         * `arabic_excerpt`: *"يشاركون في نقاش هادئ وبنّاء يتسم بالاحترام للوصول إلى أفضل الحلول."*
         * `assessment`: Approved across all criteria (`4/4`).
       * **Extracted Contrastive Signal**: Shifting from literal lexical retention (*`نقاش صحي`*) to cultural value substitution (*`نقاش هادئ وبنّاء يتسم بالاحترام`*) directly resolved the cultural fit and fluency failure while preserving the original communicative intent.
  2. **Insight Induction (Agent A2 - Insight Curator)**:
     * Analyzes comparison packets to infer general, reusable glocalization insights across domains.
     * *Example Induced Rule*: *"When translating expressions describing collaborative discourse (such as 'healthy debate'), avoid medical/hygiene calques (e.g., 'نقاش صحي'). Instead, localize using authentic Arab deliberative etiquette emphasizing calm, constructive consultation and mutual respect (e.g., 'نقاش هادئ وبنّاء يتسم بالاحترام')."*
  3. **Rule Audit & Transactional Memory Updates (Agent A5 - Insight Reviewer)**:
     * Independently audits each operation proposed by Agent A2 against five verification gates (`evidence_support`, `applicability`, `scope`, `preservation`, and `contradiction_handling`) before committing atomic versioned updates to memory:
       * **`ADD` (Create New Rule — `version = 1`, `importance = 2`)**:
         * *Mechanism*: Triggered when a comparison packet reveals a genuinely novel glocalization principle (verified against duplicate fingerprints).
         * *Example on `Pair 000020`*: From packet `cmp_failure_success_000020`, Agent A2 proposes `ADD` citing `support_experience_ids: ["exp_att_000020_2"]`. Agent A5 verifies all 5 checks pass, and the controller commits the deliberative etiquette rule as `version: 1` with initial `importance: 2`.
       * **`UPVOTE` (Reinforce Existing Rule — `version = v + 1`, `importance = +1`)**:
         * *Mechanism*: Triggered when a subsequent task succeeds specifically by applying an existing rule; appends the new `support_experience_ids` and increments importance.
         * *Example*: When a later practice trial (e.g., adapting *"a healthy argument during the committee meeting"*) succeeds by generating *`حوار هادئ وبنّاء`*, Agent A2 proposes `UPVOTE` on the rule (`expected_version: 1`). Once audited by Agent A5, the rule advances to `version: 2` with `importance: 3` and expanded cross-group support.
       * **`EDIT` (Refine Wording or Boundary Conditions — `version = v + 1`, `importance = +1`)**:
         * *Mechanism*: Triggered when new evidence requires clarifying the rule's `statement`, `applies_when`, or `avoid_when` exceptions without discarding the core insight.
         * *Example*: When encountering a clinical/medical text where *"healthy"* literally refers to physical health or sanitation, Agent A2 proposes `EDIT` (`expected_version: 2`) to sharpen the `avoid_when` boundary (*"Do not apply when 'healthy' literally modifies clinical, medical, or nutritional terms"*), advancing the rule to `version: 3` (`importance: 4`).
       * **`DOWNVOTE` (Penalize or Retire Flawed Rule — `version = v + 1`, `importance = -1`)**:
         * *Mechanism*: Triggered when empirical evidence proves that following an existing rule directly caused a localization failure; records `counterevidence_ids` and decrements importance (demoting the rule to `provisional` on unresolved counterevidence, or retiring it to `status: "retired"` when `importance` reaches `0`).
         * *Example*: If an over-generalized rule causes an agent to substitute a historical or domain-critical term that should have been retained, Agent A2 proposes `DOWNVOTE` citing the failed attempt as counterevidence, reducing its `importance` by `1` until it is edited or retired.
  4. **Release Compilation**:
     * Combines the seed demonstrations, verified practice solutions, and consolidated insight rules into a unified release.
* **Output**:
  * **Memory Release $M_1$**: An active, evolved memory containing both episodic demonstration exemplars and high-level inductive adaptation rules.

---

## Phase 2: Online Inference 
* **Input**:  
  * New, unseen English text and Insights $M_1$**.
* **Process**: 
  1. **Text Preparation & Chunking**:
     * Cleans and splits the input text into paragraphs. If the document is long, it breaks it into overlapping sections so each part is processed with its surrounding context intact.
  2. **Analyzing the Source Text (Source Profiling)**:
     * Reads the English text to understand its main purpose, topic domain, core facts that must not be changed, and cultural or idiomatic expressions.
  3. **Retrieving Examples and insights from Memory ($M_1$)**:
     * **Similar Past Examples (Demonstrations)**: Searches $M_1$ to find the most similar previously glocalized English-Arabic examples from the same domain, giving the planner concrete models of how similar expressions were adapted before.
     * **Learned Glocalization insights (Insights)**: Retrieves the general adaptation insights learned in Stage 4, ordered from highest to lowest importance, so the planner knows which cultural principles to apply and which literal translation mistakes to avoid.
     * **Packaging for Planning**: Combines the matched examples and insights into a single guidance package for the planner (if prompt space is limited, it prioritizes keeping the learned insights and drops the least similar examples first).
  4. **Creating the Adaptation Plan**:
     * Decides how to handle each identified cultural expression before writing any Arabic:
       * **`substitute`**: Replaces a foreign idiom or custom with a natural, culturally resonant Arabic equivalent when supported by a retrieved example or learned rule from $M_1$.
       * **`retain`**: Keeps the original concept when it is a core fact, or as a safe fallback if there is not enough evidence in memory to justify changing it.
     * Ensures that any repeated terms across a long document are translated consistently, then locks the plan.
  5. **Generating the Arabic Text**:
     * Writes fluent Modern Standard Arabic that strictly follows the locked adaptation plan.
     * Records the exact Arabic words used for each planned cultural item and combines all sections into one complete Arabic text.
  6. **Direct Release (Quality Evaluation Bypassed in Online Inference)**: 
     * In deployment inference (such as the Glocalization Workbench), the generated Arabic text is released directly once validated against the frozen plan. The separate Quality Evaluation step is skipped during inference to eliminate unnecessary latency and reduce model calls.
     * *(Note: Quality Evaluation remains fully active in the Practice Loop during experiential learning to audit attempts, drive self-corrections, and discover new insights).*

## Worked example

This end-to-end trace demonstrates how Pair progresses through **Attempt 1 (Failure)**, **Diagnostic Reflection**, and **Attempt 2 (Self-Corrected Success)** during experiental learning.

  * **English Source**: *"They engage in healthy debate to reach optimal solutions."*
  * **Quarantined Gold Glocalized Arabic Reference**: *"يشاركون في نقاش هادئ وبنّاء يتسم بالاحترام للوصول إلى أفضل الحلول."*

### Source Profiling (Agent A3 - Source Profiler)
Before planning, Agent A3 analyzes the English source and extracts the communicative purpose, requirements, and culturally sensitive items:
purpose": "Describe collaborative problem-solving through constructive group discussion."
requirements: "Preserve the core meaning of productive group deliberation aimed at finding the best outcomes."
item_id: "c1":"healthy debate"
"item_id": "c2": "optimal solutions"

### Loop 1: First Attempt (Outcome: `failure`)

#### 1. Planner Output 
With no prior insights against conversational calques in $M_0$, the planner defaults to literal retention (`retain`) and binds direct lexical equivalents:
 c1: "strategy": "retain"; "rationale": "Retain the phrase 'healthy debate' via direct translation into Arabic."
c2 : "strategy": "retain", "rationale": "Retain 'optimal solutions' via direct translation."

#### 2. Generator Output 
Agent A4 synthesizes the Arabic text and logs exact character quote locators: 
"text_ar": "يشاركون في نقاش صحي للوصول إلى حلول مثالية."
c1: "نقاش صحي"
C2: "حلول مثالية"


#### 3. Evaluator Output 
Agent A5 audits the candidate against quality criteria and the gold glocalized reference, flagging the clinical calque (`نقاش صحي`) and unnatural phrasing (`حلول مثالية`):
C1:"status": "fail", "explanation": "'نقاش صحي' is a literal English calque. In Arabic, 'صحي' refers to physical health or sanitation, not intellectual discourse."
c2: "status": "fail",  "explanation": "'حلول مثالية' connotes idealistic/utopian perfection rather than practical optimal outcomes.",

#### 4. Reflector Feedback 
Triggered by the failed evaluation, Agent A6 inspects Attempt 1's failure and synthesizes a concise, actionable diagnostic instruction for Attempt 2:
 "mode": "reflect_failure",
  "reflection": "CALQUE_DETECTED: Attempt 1 failed because 'نقاش صحي' is an awkward medical/hygiene calque of 'healthy debate', and 'حلول مثالية' implies utopian perfection. In Attempt 2, switch strategy from 'retain' to 'substitute': localize 'healthy debate' into authentic Arab deliberative values emphasizing calm, constructive dialogue and mutual respect ('نقاش هادئ وبنّاء يتسم بالاحترام'), and render 'optimal solutions' pragmatically as 'أفضل الحلول'."

### Loop 2: Second Attempt (Outcome: `success`)

#### 1. Planner Output 
Conditioned on Agent A6's reflection and Agent A5's findings, Agent A3 revises its plan from `retain` to `substitute`:
"strategy": "substitute","rationale": "Substitute the English metaphor 'healthy debate' with phrasing rooted in Arab communicative etiquette (أدب الحوار) emphasizing calm, constructive, and respectful dialogue."

{
  "decisions": [
    {
      "decision_id": "d1",
      "item_id": "c1",
      "strategy": "substitute",
      "counterpart_ar": "نقاش هادئ وبنّاء يتسم بالاحترام",
      "canonical_concept": "calm, constructive, and respectful deliberation",
      "term_policy": "fixed",
      "preserve_requirement_ids": ["r1"],
      "rationale": "Substitute the English metaphor 'healthy debate' with phrasing rooted in Arab communicative etiquette (أدب الحوار) emphasizing calm, constructive, and respectful dialogue."
    },
    {
      "decision_id": "d2",
      "item_id": "c2",
      "strategy": "substitute",
      "counterpart_ar": "أفضل الحلول",
      "canonical_concept": "the best practical solutions",
      "term_policy": "fixed",
      "preserve_requirement_ids": ["r1"],
      "rationale": "Replace literal 'حلول مثالية' with the natural Arabic expression 'أفضل الحلول' to convey pragmatic optimization."
    }
  ],
  "terminology": [
    {
      "source_concept": "healthy debate",
      "preferred_ar": "نقاش هادئ وبنّاء يتسم بالاحترام",
      "item_ids": ["c1"],
      "policy": "fixed"
    },
    {
      "source_concept": "optimal solutions",
      "preferred_ar": "أفضل الحلول",
      "item_ids": ["c2"],
      "policy": "fixed"
    }
  ],
  "reflection": "Absorbed A6 diagnostic feedback: replaced literal calque 'نقاش صحي' with culturally resonant deliberative phrasing 'نقاش هادئ وبنّاء يتسم بالاحترام' and refined 'حلول مثالية' to 'أفضل الحلول'.",
  "unresolved_issues": []
}
``` -->

#### 2. Generator Output 
Agent A4 executes `PlanPayload` v2 and produces fluent Modern Standard Arabic with exact realization spans:
"text_ar": "يشاركون في نقاش هادئ وبنّاء يتسم بالاحترام للوصول إلى أفضل الحلول."


#### 3. Evaluator Output 
Agent A5 re-evaluates the self-corrected candidate, confirming full alignment with cultural norms, Arabic fluency, and reference compatibility:
 "pass"

---

## How Retrieval Similarity and Insight Importance Work

1. **How Similarity Is Calculated for Examples vs. Insights**:
   - **Past Examples (Demonstrations)**: First filtered by matching domain/scope, then the full English source text is converted into a normalized semantic vector using the **`all-mpnet-base-v2` sentence-transformer** embedding model and ranked by **cosine similarity** (via FAISS) against the new English text to pick the top-$k$ ($k=2$) most similar examples from distinct document groups.
   - **Learned Rules (Insights)**: Rather than matching exact words, all **active, domain-compatible rules** in $M_1$ are retrieved so general cultural principles apply even when the exact wording of a new sentence is different.
2. **How Importance Is Measured and Ordered**:
   - **Initial Creation (`ADD`)**: Starts at **`2`** points.
   - **Proven Useful Again (`UPVOTE`)**: Gains **`+1`** point each time a subsequent trial succeeds by applying the rule.
   - **Refined (`EDIT`)**: Gains **`+1`** point when updated with clearer wording or boundary conditions.
   - **Penalized (`DOWNVOTE`)**: Loses **`-1`** point if following the rule causes an error, and is automatically **retired** if its score reaches **`0`**.
   - **Ordering**: During inference, rules are sorted from **highest to lowest importance**, placing the most proven rules first and protecting them if prompt space is tight.
