# Ruby and Cucumber use cases

Scored on 19 November 2026 against the four indexed repositories, at the same commits as `eval/FINDINGS.md`. Query, Impact, and Search used the page path: code index, reciprocal rank fusion, and the question planner. Written answers used `gpt-4o-mini`. The two feature-writing scores were re-checked the same day on Features, after that button was added.

The note being scored asks a retrieval system to write tests, explain behaviour, review the suite, diagnose failures, and onboard people. This app is a lookup over indexed Ruby and Cucumber. Documentation, a named feature trace, and Impact do that job. Features finds an existing step, or the steps to reuse when writing a feature, and quotes only lines that are already in the suite. Healing the suite from a description, and auditing it for smells, remain outside what the page does.

Overall: **6/10**.

Recall figures below are from `eval/FINDINGS.md` (17 November 2026, 95 questions, top 8, planner off). They are the retrieval ceiling for Query. The live notes for Query are what the page returned with the planner on. The feature-writing notes are from Features.

| Use case | Score | What a live query did |
|---|---|---|
| Write a feature from plain English | 7/10 | Features quotes the fee scenarios and says the refund request has no step |
| Write a matching step definition | 7/10 | Features finds `fee_steps.rb` for Then, And, Given, and When |
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

### Write a feature from plain English — 7/10

On Features, against `hmcts/hwf-publicapp`:

```text
Write a Cucumber feature for an applicant who already paid the fee and wants a refund. The payment date cannot be in the future or more than three months ago. Reuse existing step phrasing.
```

The search that runs is “an applicant who already paid the fee and wants a refund. The payment date cannot be in the future or more than three months ago.” With 12 passages, every hit is under `features/`. The first is `features/fee.feature`, scenario “Date exceeding 3 months”, then `FeePage#submit_fee_yes` and `features/step_definitions/fee_steps.rb`. The written answer quotes those existing steps, including “Date within the last 3 months”, and says the suite has no Gherkin line for the refund request itself. It does not contain `Given the applicant has paid the fee`.

The same question on Query still plans down to `refund` and can miss `features/fee.feature`. Use Features for this request.

### Write a matching step definition — 7/10

On Features, against `hmcts/hwf-publicapp`, each of these returns the same passages:

```text
Then I should see this date can't be in the future error message
```

```text
And I should see this date can't be in the future error message
```

`Given` and `When` do the same. The keyword is not part of the match, so a second Then, Given, or When written as And is included. The passages are `features/fee.feature` (scenario “Date is in the future”), both probate features, and `features/step_definitions/fee_steps.rb`.

Features finds the existing definition. It does not write a new step method. On Query, the same Then line is still planned into `error message` and `date future`, and `fee_steps.rb` can fall out of the top 8. Unplanned, the 13 pasted-step questions score Recall@8 **0.92** on the code index.

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

## Features

**Status:** implemented. Query is unchanged, so a pasted step or a “write a feature” request on Query is still planned. Features is the page button, `POST /api/features`, `rag features`, and the `features` MCP tool. The code is `app/rag/features.py`.

Two requests used to fail for the same reason. Query searched the instruction, a short planned query crowded the top 8, and the answer wrote Gherkin that is not in the index. A pasted Then step was planned into `error message` and `date future`, and `fee_steps.rb` fell out of the top 8. The refund question was planned into `refund`, `features/fee.feature` was absent, and the answer invented `Given the applicant has paid the fee on "2023-01-01"`.

Features does this instead:

1. A Gherkin line is matched under `features/` on the step text, then followed into its step definition. Given, When, Then, And, and But are the same step, so a second Then written as And is included.
2. A request to write, draft, or generate a feature, or to reuse existing steps, drops that instruction and searches the behaviour sentence on the code index. Planning stays off.
3. The written answer may quote a Gherkin line only when that line is in a passage. A requested check with no such line is named, and no substitute step is written.
4. Paths that start with `features/` are listed before other directories. The order inside each group stays the relevance order.

Checked on `hmcts/hwf-publicapp` after the app was restarted:

- `Then I should see this date can't be in the future error message`, and the same line with And, Given, or When, each return `features/fee.feature`, both probate features, and `features/step_definitions/fee_steps.rb`.
- The refund question above returns `features/fee.feature` first (scenario “Date exceeding 3 months”). The first 12 passages are all under `features/`. The written answer quotes the fee scenarios and says the refund request has no step. It does not contain `Given the applicant has paid the fee`.

That is the move from 2/10 and 3/10 to 7/10. The remaining gap is that Features will not write a new step method, and a behaviour with no scenario comes back as a named gap.

### What this leaves alone

Healing from a description, smell audits, and spec drafting stay at their scores above. Those still need a different route: Impact for a named page, and a question that states the rule rather than the instruction to write.
