# Ruby and Cucumber use cases

Scored on 19 November 2026 against the four indexed repositories, at the same commits as `eval/FINDINGS.md`. Each query used the page path: code index, reciprocal rank fusion, and the question planner. Written answers used `gpt-4o-mini`.

The note being scored asks a retrieval system to write tests, explain behaviour, review the suite, diagnose failures, and onboard people. This app is a lookup over indexed Ruby and Cucumber. Documentation, a named feature trace, and Impact do that job. Writing a new feature, healing the suite from a description, and auditing it for smells do not.

Overall: **5/10**.

Recall figures below are from `eval/FINDINGS.md` (17 November 2026, 95 questions, top 8, planner off). They are the retrieval ceiling. The live notes are what the page returned with the planner on.

| Use case | Score | What a live query did |
|---|---|---|
| Write a feature from plain English | 2/10 | Invented new steps and missed the existing fee scenarios |
| Write a matching step definition | 3/10 | A pasted step missed `fee_steps.rb`; an exact phrase found it |
| Heal steps after a UI change | 5/10 | Naming `DobPage` listed the scenarios; describing the change did not |
| Answer what the system does, and how it is tested | 7/10 | Date-of-birth rules landed on `Forms::Dob` and `DobPage` |
| Trace a scenario down to Ruby | 8/10 | Naming the feature file reached the step file and `DobPage#valid_dob` |
| Find test-suite smells | 2/10 | An xpath question on the ET suite returned feature titles |
| Say whether a behaviour is already covered | 6/10 | The 3-month fee-date rule came back as `features/fee.feature` |
| Pin a Cucumber failure to the step | 5/10 | Quoting the expected text found the step file and the locale string |
| Explain that failure in plain English | 6/10 | The reply explained the check, then asserted a cause the passages do not show |
| Onboarding questions | 7/10 | A named class is reliable; a vague permissions question hit the wrong file |
| Draft a spec or release note | 4/10 | “How is an application submitted?” led with building the form, not posting it |

## 1. Natural language test generation and maintenance

### Write a feature from plain English — 2/10

On `hmcts/hwf-publicapp`:

```text
Write a Cucumber feature for an applicant who already paid the fee and wants a refund. The payment date cannot be in the future or more than three months ago. Reuse existing step phrasing.
```

The planner added the query `refund`. Passages were refund views, `Views::Summary#refund_text`, and `Forms::Fee#received_and_refund_data`. `features/fee.feature` was absent. The written answer then produced `Given the applicant has paid the fee on "2023-01-01"` and said the passages contained no existing phrasing to reuse. Those steps are not in the suite.

The query that finds the scenarios worth copying:

```text
Do existing Cucumber features cover the rule that a fee payment date cannot be in the future or more than three months ago?
```

First hit: `features/fee.feature`, scenario “Date exceeding 3 months”.

### Write a matching step definition — 3/10

Pasting the step as an ordinary question on `hmcts/hwf-publicapp`:

```text
Then I should see this date can't be in the future error message
```

The planner added `error message` and `date future`. The top 8 were `FeePage#future_date`, the fee and probate scenarios, and the RSpec page specs. `features/step_definitions/fee_steps.rb` was outside that list. Unplanned, the same 13 pasted-step questions score Recall@8 **0.92** on the code index.

Quoting the step finds both definition files:

```text
look under /features for "this date can't be in the future"
```

Hits: `features/fee.feature`, both probate features, `fee_steps.rb`, and `probate_steps.rb`.

### Heal steps after a UI change — 5/10

Describing the change failed. “Which step definitions fill in the date of birth and would need updating if the date fields changed?” returned homepage, summary, and dependent steps.

Naming the page routes to Impact and lists the features that reach it. On `hmcts/hwf-publicapp`:

```text
What is affected if I change DobPage, for example valid_dob?
```

Definition: `features/support/page_objects/dob_page.rb`. References run through `dob_steps.rb`, including address scenarios that only pass through that page. That is the blast radius. Impact finds **100%** of expected callers on the 29 impact questions. The app stops at the list.

## 2. Intelligent code and test documentation

### What the system does, and how it is tested — 7/10

On `hmcts/hwf-publicapp`:

```text
What age checks are applied to the applicant's date of birth, including the rule about both partners being over pension age of 66?
```

Top passages included `Forms::Dob#not_under_66?`, `#not_over_66?`, `#validate_dob_ranges`, and `DobPage#valid_over_66_dob`. `config/environments/production.rb` was in the same list.

The same shape is uneven. On `hmcts/hwf-staffapp`, “Where are user permissions validated, and how is it tested in Cucumber?” put `config/initializers/permissions_policy.rb` first. That file is the browser permissions policy. `UserPolicy#edit?` was fifth, and no Cucumber file appeared. Plain-English recall on the code index is **0.48** overall and **0.29** on the staff app.

Naming the symbol is the dependable form. Identifier questions score **0.97**:

```text
Where is NI_NUMBER_REGEXP defined and used to validate the applicant's and partner's National Insurance numbers?
```

### Trace a scenario down to Ruby — 8/10

On `hmcts/hwf-publicapp`:

```text
how does features/dob/dob_single.feature enter the date of birth
```

The result included the scenario “Entering a valid date of birth”, `features/step_definitions/dob_steps.rb`, and `DobPage#valid_dob`. Form methods from `Forms::Dob` ranked above that chain.

The reverse direction is an Impact target on the same repository:

```text
IncomePeriodPage#submit_income
```

## 3. Automated code review and refactoring

### Find test-suite smells — 2/10

On `hmcts/et-full-system`:

```text
Where do Cucumber page objects use fragile xpath or css selectors?
```

The planner searched for “Cucumber page” and returned feature titles (`diversity_landing_page.feature`, `et1_employment_details_page.feature`). Eight passages cannot survey a suite.

### Say whether a behaviour is already covered — 6/10

On `hmcts/hwf-publicapp`:

```text
which features cover date of birth
```

Feature files only: `features/dob/dob_single.feature`, `features/dob/dob_married.feature`, then nearby date scenarios on the fee and probate pages.

For a named class, Impact is the coverage check. On `hmcts/hwf-publicapp`:

```text
What calls SubmitApplication and which tests would I retest?
```

It returned `app/services/submit_application.rb`, `submit_application_spec.rb`, `SubmissionsController#submit_service`, the controller spec, and `HealthStatus::HealthCheck#check_submission_api!`. There is no way to hand it a diff.

## 4. Intelligent debugging and failure analysis

### Pin a Cucumber failure to the step — 5/10

On `hmcts/hwf-publicapp`, when the log quotes a step the suite contains:

```text
The Cucumber run failed with: expected to find text "this date can't be in the future" on the fee page. Which step definition checks that message?
```

Hits included `config/locales/en.yml`, `features/fee.feature`, both probate features, `fee_steps.rb`, and `probate_steps.rb`. A stack frame that names a class is better sent through Impact, as with `SubmitApplication` above. A long log has to share the passage list with the code.

### Explain that failure in plain English — 6/10

The written answer said the test checks that a future date is rejected, and it cited the locale string, the feature step, and `fee_steps.rb`. It then said the application did not display the message. The passages show what the test looks for. They do not show a run in which the message was missing.

## 5. Onboarding and developer productivity

### Questions from a new hire — 7/10

On `hmcts/hwf-publicapp`:

```text
where is the application submitted
```

`SubmitApplication#build_params` was fifth and `SubmissionsController#submit` was seventh, behind the session accessor and `OnlineApplicationBuilder`.

On `ministryofjustice/c100-application`:

```text
How does the service choose the family court from the postcode where the children live, and what happens for Scottish or Northern Irish postcodes?
```

The CourtPostcodeChecker spec for Scottish postcodes and `OpeningDecisionTree#eligable_court` were in the top 8. The checker service file itself was not.

Naming the class fixes the staff-app permissions question. On `hmcts/hwf-staffapp`:

```text
What is affected if I change UserPolicy?
```

Impact returned `app/policies/user_policy.rb`, `spec/policies/user_policy_spec.rb`, and a users index view spec.

### Draft a spec or release note — 4/10

On `hmcts/hwf-publicapp`:

```text
Draft a short technical note of how an online application is submitted, from the controller through to the external service.
```

The top passages were `QuestionsController#process_form_and_online_application` and `OnlineApplicationBuilder`. A note written from those would describe saving the form. `where is the application submitted`, then Impact on `SubmitApplication`, is the pair that reaches the post.

## Plan: stop invented Cucumber steps

**Status:** implemented on the Features button. Query is unchanged, so a pasted step on Query is still planned. Features (`POST /api/features`) skips the planner, matches a Gherkin line under `features/`, searches a writing request as the behaviour sentence, and tells the answer model to quote only steps that appear in the passages.

Two requests failed for the same reason. The page searched the instruction, let a short planned query crowd the top 8, and the answer model wrote Gherkin that is not in the index.

1. Pasted step `Then I should see this date can't be in the future error message`. `question_needs_plan` in `app/rag/plan.py` is true for an unquoted step, so the planner added `error message` and `date future`. `retrieve` in `app/rag/search.py` searches each planned query and merges them. `fee_steps.rb` fell out of the top 8. The same question, unplanned, is one of the 13 step questions at Recall@8 0.92. Quoting the text (`look under /features for "..."`) already takes the phrase path in `quoted_phrases` and finds `fee_steps.rb`.
2. “Write a Cucumber feature … Reuse existing step phrasing.” The full sentence was searched, plus `refund`. `features/fee.feature` was absent. The answer instructions in `app/rag/answer.py` say not to invent behaviour, and the model still emitted three new scenarios. It also said the passages had no step phrasing, which was true of the passages it was given.

The quoted-phrase path and the feature-list path already skip the planner. A Gherkin line and a “write a feature” request should join them.

### Goal

- A pasted step is searched as that step. On the page path, `pub-step-1` in `eval/hwf-publicapp.jsonl` returns `features/step_definitions/fee_steps.rb` inside the top 8.
- The refund feature request returns `features/fee.feature` inside the top 8. The written answer quotes a step line from a passage, or names the check that has no step. It does not contain `Given the applicant has paid the fee`.
- That moves “write a feature from plain English” from 2/10 to about 6/10: the user gets the existing steps, and a scenario only where those steps exist.

### Work items

Do these in order.

#### 1. Treat a Gherkin line as an exact phrase

**Why it fails today.** `quoted_phrases` only sees text the user has wrapped in quotes. A line that already starts with `Given`, `When`, `Then`, `And`, or `But` is planned like any other sentence. The extra queries are merged in `retrieve` and push the step definition down.

**Change.**

- Add a detector next to `quoted_phrases` that returns each Gherkin line in the question, keywords included, as a phrase.
- In `question_needs_plan`, return false when that detector finds a line, the same way it already returns false for quotes, feature paths, and “which features”.
- When those lines are present, search them with the phrase path, scoped under `features/`, and follow a matched step into its definition the way `follow_matched_steps` already does for a quoted step.

**Prove it.** A unit test with the `pub-step-1` text asserts that planning is skipped and the phrase is the step line. Then:

```powershell
docker compose run --rm --no-deps -T --entrypoint python -v "${PWD}/eval:/eval" rag /eval/run_eval.py --questions /eval/hwf-publicapp.jsonl --repo hmcts/hwf-publicapp --query-path --details
```

`pub-step-1` must list `features/step_definitions/fee_steps.rb`. Step Recall@8 on that query-path run should stay at or above the unplanned **0.92**.

#### 2. Search the behaviour, not the instruction to write a feature

**Why it fails today.** The sentence begins with “Write a Cucumber feature” and ends with “Reuse existing step phrasing.” Those words are what the short planned query keeps (`refund`), and the embedding of the whole instruction does not surface `features/fee.feature`. The coverage question that states only the rule already hits that file first.

**Change.**

- Detect a drafting request: the question asks to write, draft, or generate a feature, or to reuse existing steps.
- Drop that framing and search the remaining behaviour sentence on the code index, as one query, with planning left off.
- Also run a feature-file phrase search for any quoted or Gherkin text inside the request, so a constraint that already has a scenario is not outvoted by a refund view.

**Prove it.** A unit test on the refund question asserts the searched text keeps “payment date”, “future”, and “three months”, and drops “Write a Cucumber feature” and “Reuse existing step phrasing”. A query-path check against `hmcts/hwf-publicapp` must return `features/fee.feature` in the top 8.

#### 3. Quote steps from passages, or say the step is missing

**Why it fails today.** `_QUERY_INSTRUCTIONS` forbids inventing files, methods, or behaviour. A drafting question still gets a normal answer, and the model fills the gap with new Gherkin, then admits the steps were not retrieved.

**Change.** When the detector from item 2 matches, append an instruction: a scenario line is allowed only when that line appears in a passage; quote it with the path and line number; if a requested check has no such line, name the check and stop. Do not write a substitute step.

**Prove it.** Re-run the refund question through `POST /api/query` with `answer: true` on `hmcts/hwf-publicapp`. The reply must quote a line from `features/fee.feature` or say which check has no step. It must not contain `Given the applicant has paid the fee`.

### What this leaves alone

Healing from a description, smell audits, and spec drafting stay at their scores above. Those need a different route: Impact for a named page, and a question that states the rule rather than the instruction to write. This plan covers the requests that currently invent a test suite.
