# Text and code index evaluation

Run on 17 November 2026. The question is how well the text index, the code index, and both together find the right files for a query, and how that compares with the **Impact** button for finding callers.

## Summary

- Use the **code** index with reciprocal rank fusion for Query. It had the best overall recall in three of the four repositories, and the best overall result across all 95 questions.
- Plain-English questions are where the indexes differ most, and the code index wins. The text index tends to return locale files such as `config/locales/en.yml`, because their English UI wording matches English questions.
- Questions that name a method, class, or constant work with every setting.
- A pasted Gherkin step needs the keyword ranking to carry real weight. Reciprocal rank fusion does that for the text or code index alone. With both indexes, the score sum works better.
- Use the **Impact** button, not Query, to find callers. It found 91% of the expected callers. Search found about 30% with any setting.

## What was measured

| Repository | Branch | Commit | Files | Chunks | Questions |
|---|---|---|---|---|---|
| hmcts/hwf-staffapp | master | 19fd6b35a125 | 1354 | 12491 | 20 |
| hmcts/hwf-publicapp | master | b3063dc39ed1 | 477 | 2913 | 25 |
| hmcts/et-full-system | develop | 4177fdb444f2 | 346 | 2522 | 25 |
| ministryofjustice/c100-application | master | e009daaac8fd | 1764 | 10066 | 25 |

The questions are in this folder, one file per repository. Each expected path was checked against the source at the commit above and is present in the index. There are four kinds of question:

- **concept** (34): plain English with no code names, such as "How long can an unfinished application sit idle before the saved answers expire?"
- **identifier** (19): names a method, class, or constant, or quotes a line of Ruby with no names in it.
- **impact** (29): asks what calls a named method or class. The expected paths are the callers, not the definition.
- **step** (13): a Gherkin step copied from a feature file. The expected path is the step definition file.

`run_eval.py` searches each question once, without the question-planning model, and keeps the top 8 passages. Recall@8 is the share of expected files in those 8. MRR (mean reciprocal rank) is 1 divided by the rank of the first expected file, averaged over the questions. Reciprocal rank fusion used k = 60.

The text index is `BAAI/bge-small-en-v1.5`, which reads up to 512 tokens. The code index is `jinaai/jina-embeddings-v2-base-code`, which reads up to 2048 tokens of each chunk here, embedded in batches of 4. Text in a chunk beyond 2048 tokens is not seen by the code index.

## Results across all four repositories

Recall@8 / MRR, weighted by the number of questions.

| Kind of question | Text, rank fusion | Text, score sum | Code, rank fusion | Code, score sum | Both, rank fusion | Both, score sum |
|---|---|---|---|---|---|---|
| Concept (34) | 0.21 / 0.09 | 0.21 / 0.09 | **0.48 / 0.41** | 0.48 / 0.41 | 0.42 / 0.36 | 0.42 / 0.36 |
| Identifier (19) | 0.97 / 0.89 | 0.92 / 0.83 | 0.97 / 0.90 | 0.97 / 0.83 | 0.97 / 0.94 | 0.97 / 0.90 |
| Impact, by search (29) | 0.28 / 0.31 | 0.28 / 0.31 | 0.31 / 0.27 | 0.31 / 0.27 | 0.33 / 0.29 | 0.33 / 0.29 |
| Step (13) | 0.85 / 0.43 | 0.31 / 0.23 | **0.92 / 0.47** | 0.54 / 0.38 | 0.54 / 0.32 | 0.85 / 0.58 |
| **Overall (95)** | 0.47 / 0.37 | 0.39 / 0.33 | **0.59 / 0.48** | 0.53 / 0.45 | 0.52 / 0.45 | 0.56 / 0.48 |

## Results by repository

Overall recall@8 / MRR with reciprocal rank fusion.

| Repository | Text | Code | Both |
|---|---|---|---|
| hwf-staffapp | 0.46 / 0.30 | **0.58 / 0.39** | 0.52 / 0.32 |
| hwf-publicapp | 0.45 / 0.27 | **0.67 / 0.54** | 0.57 / 0.51 |
| et-full-system | **0.58 / 0.53** | 0.52 / 0.48 | 0.57 / 0.52 |
| c100-application | 0.39 / 0.35 | **0.57 / 0.48** | 0.41 / 0.43 |

Plain-English questions only, recall@8 / MRR with reciprocal rank fusion.

| Repository | Text | Code | Both |
|---|---|---|---|
| hwf-staffapp (7) | 0.07 / 0.05 | 0.29 / 0.21 | 0.29 / 0.09 |
| hwf-publicapp (9) | 0.22 / 0.06 | 0.67 / 0.65 | 0.61 / 0.63 |
| et-full-system (9) | 0.28 / 0.15 | 0.35 / 0.35 | 0.35 / 0.39 |
| c100-application (9) | 0.22 / 0.11 | 0.56 / 0.39 | 0.39 / 0.27 |

`et-full-system` is a Cucumber suite with little application code, and it is the one repository where the text index did slightly better overall. `hwf-staffapp` had the hardest plain-English questions for both indexes.

## Findings

### Plain-English questions favour the code index

This was expected to go the other way. The text index's first hit for a plain-English question is often a locale or messaging file, such as `config/locales/en.yml` or `features/support/messaging/et1_en.yml`. Those files hold the English text shown on screen, so they match the wording of the question but not the code that does the work. The code index more often finds that code. For example, it finds `app/models/forms/dob.rb` for the date-of-birth rules.

### Both indexes together is not better than code alone

With `both`, the two vector rankings are merged. For plain-English questions the weaker text ranking pulls good code hits down. In `c100-application` that lowered plain-English recall from 0.56 to 0.39.

### Keyword fusion matters for pasted step text

For most questions the two fusion modes gave the same or nearly the same result. Pasted Gherkin steps were the exception. Step text is a run of exact words, so the keyword ranking finds the step definition well.

- With one index, reciprocal rank fusion gives the keyword ranking an equal vote with the vector ranking. Recall was 0.85 with the text index and 0.92 with the code index. With the score sum, the cosine similarity outweighs the keyword score, and recall fell to 0.31 and 0.54.
- With both indexes, reciprocal rank fusion puts the keyword ranking up against two vector rankings, so it is outvoted. Recall was 0.54. The score sum adds the keyword score on top of the merged vector ranking, and recall was 0.85.

### Search is a poor way to find callers

Asked "what calls X", every search setting mostly returns the definition of X and its spec, not the callers. Recall was about 0.3 with any setting.

### The Impact button finds callers well

The same 29 targets were sent to `POST /api/impact`, which follows the stored call edges and does not use either vector index. It found 91% of the expected callers. 25 targets found every expected caller, two found some, and two found none. All four misses are gaps in the call graph:

| Target | Found | Why callers were missed |
|---|---|---|
| `C100App::PaymentsFlowControl` (c100-application) | 3 of 4 | Inside `module C100App` the caller writes `PaymentsFlowControl.new` without the `C100App::` prefix, in `ApplicationDecisionTree#proceed_to_payment`. |
| `C100App::OnlineSubmissionQueue` (c100-application) | 2 of 3 | The same unprefixed reference, in `PaymentsFlowControl#confirmation_url`. |
| `ResolverService#return` (hwf-staffapp) | 0 of 3 | Callers write `ResolverService.new(...).return`, and `return` is also a Ruby keyword, so the call is not linked. |
| `ResponsesPage` (et-full-system) | 0 of 2 | Steps reach the page object through the accessor `admin_pages.responses_page`, never by its class name. |

### Questions that name code work with any setting

Identifier questions scored 0.92 to 0.97 recall in every setting, usually with the right file first. That includes questions that quote a line of Ruby with no names in it. The only miss was a validator used by a form, where the form file was not in the top 8.

## Recommendations

1. Make `code` the default index for Query, and `rrf` the default fusion with it.
2. If `both` is kept as an option, pair it with the score sum.
3. Point users to the **Impact** button for "what calls X" or "what is affected" questions.
4. Fix the call-graph gaps above: resolve an unprefixed constant from inside its own namespace, link a method called `return` when it has a receiver, and link SitePrism accessors such as `responses_page` to their page class.

## Limits

- The question set is small. One plain-English question in a repository is 0.11 to 0.14 of its plain-English score, so treat differences under about 0.1 as noise. The code index's lead on plain-English questions is large and appears in all four repositories.
- Expected paths were chosen by reading the code. A file the search returns that is not in the list counts as a miss, even when it also answers the question.
- Search was measured without the question-planning model that the page uses before searching. The planner rewrites a question into a few short searches, which may narrow the gap between the indexes.

## Rerun

Index the repositories one at a time, then run each file in the app image:

```powershell
docker compose run --rm --no-deps -T --entrypoint python -v "${PWD}/eval:/eval" rag /eval/run_eval.py --questions /eval/hwf-staffapp.jsonl --repo hmcts/hwf-staffapp --details
```

Replace the file and repository for `hwf-publicapp`, `et-full-system`, and `c100-application`.
