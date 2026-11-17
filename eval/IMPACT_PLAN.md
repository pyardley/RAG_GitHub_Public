# Plan: raise Impact caller recall toward 100%

**Status (17 November 2026):** Items 1–3 are implemented in `app/rag/edges.py` (`_EDGE_VERSION` 10). `eval/run_impact.py` scores all 29 impact questions at **mean recall 1.00**. Item 4 (softened text mention) and item 5 (related shapes) were not needed for this set.

On 17 November 2026, Impact first found **91%** of the expected callers across the 29 impact evaluation questions (25 targets perfect, 2 partial, 2 empty). Search found about 30%. This plan covers the four known misses, related patterns that will show up next, how to ship each change, and how to prove the score has moved.

Impact finds callers in two ways:

1. **Text mention.** Chunks whose content matches the target name (`_impact_rows` in `app/rag/search.py`).
2. **Call edges.** Edges stored by `app/rag/edges.py`, walked outward from the definition (`_callers_and_gaps`).

All four misses fail both routes. Fixing the edges is the lasting fix: a rebuild of edges at startup (`_EDGE_VERSION` bump) is enough; no re-ingest is needed.

## Goal

- Re-run the 29 impact cases and reach **100%** of their expected callers.
- Keep noise low: do not link bare Ruby keywords, and do not link every word ending in `_page` outside the test helpers that actually hold page objects.
- Add unit tests for each gap before rebuilding edges on the live index.

## Current score on the 29 cases

| Target | Found | Missed files |
|---|---|---|
| 25 other targets | all expected callers | — |
| `C100App::PaymentsFlowControl` | 3 of 4 | `app/services/c100_app/application_decision_tree.rb` |
| `C100App::OnlineSubmissionQueue` | 2 of 3 | `app/services/c100_app/payments_flow_control.rb` |
| `ResolverService#return` | 0 of 3 | `pending_application_closer.rb`, `accuracy_failed_reason_controller.rb`, `part_payments_controller.rb` |
| `ResponsesPage` | 0 of 2 | `et3_admin_responses_page.steps.rb`, `et3_admin_verification_steps.rb` |

Mean recall today: **0.91**. Hitting every expected path above raises it to **1.00** on this set.

## The three call-site shapes that fail

Confirmed in the indexed chunks:

```ruby
# 1. Unprefixed constant inside its own module (c100-application)
PaymentsFlowControl.new(c100_application).payment_url

# 2. Method named like a Ruby keyword, called on a constructed object (hwf-staffapp)
ResolverService.new(part_payment, current_user).return

# 3. SitePrism page reached through a pages container, not by class name (et-full-system)
admin_pages.responses_page.load
```

## Work items

Do these in order. Each item ends with a `_EDGE_VERSION` bump so an already-indexed repository rebuilds edges on the next app start.

### 1. Store and resolve constant construction (`Const.new`)

**Why it fails today.** `_CONST_CALL` records `Receiver.method`, but `new` is in `_SKIP_CALLS`, so `PaymentsFlowControl.new` creates no edge. Impact then falls back to text mention. For a qualified target such as `C100App::PaymentsFlowControl`, `identifier_regex` requires both pieces (`C100App` … `PaymentsFlowControl`), so the unprefixed name in `ApplicationDecisionTree#proceed_to_payment` is invisible.

**Change in `extract_edges`.** When the method is `new` (and only then), store an edge whose destination is the constant itself, not `Const.new`:

- Detail: `PaymentsFlowControl.new`
- Destination symbol: `PaymentsFlowControl` (leaf or as written)
- Edge type: `call`, confidence `high`

Keep skipping other methods on skipped receivers (`Rails`, `Time`, and so on). Keep skipping bare `new` with no constant receiver.

**Change in `CallIndex.targets`.** When the destination is a constant leaf (no `#`, no `.`), resolve it the way `_constant` already resolves a superclass: from the caller's namespace outward, then as a unique `::<leaf>` ending. So from inside `C100App::ApplicationDecisionTree`, `PaymentsFlowControl` becomes `C100App::PaymentsFlowControl`.

That single resolution also covers `OnlineSubmissionQueue.new` inside `C100App::PaymentsFlowControl`.

**Unit tests.**

- Inside `module C100App`, `PaymentsFlowControl.new(...)` links to `C100App::PaymentsFlowControl`.
- A second class named `PaymentsFlowControl` in another namespace is not linked when the leaf is ambiguous.
- `Time.new` and `Date.new` stay unlinked.

**Acceptance.** The two c100-application partial misses become full hits. No new edges from `Time.new` / `Date.new`.

### 2. Link a keyword-named method when it has a receiver

**Why it fails today.** Callers write `ResolverService.new(...).return`. `_RECEIVER_CALL` only matches `word.word`, so a call after `)` is invisible. Bare and inline extractors put `return` in `_RUBY_KEYWORDS` and drop it, which is correct for a real `return` statement.

**Change.** Add a pattern for a chained call after construction:

```text
([A-Z][\w:]*)\.new\b[^.\n]*\.([a-z_]\w*[?!]?)
```

For each match, store an edge to `Receiver#method` (or `Receiver.method` if it is a class method). Do this even when `method` is in `_RUBY_KEYWORDS` or `_SKIP_CALLS` that are about Capybara/AR helpers (`return` is a keyword; `call` might appear as `.call` on a proc and should stay skipped unless the receiver is a known class).

Also allow a simpler form with a local receiver that was just assigned from `Const.new`, only if it is cheap and precise. Prefer the chained form first; assignment tracking can wait.

**Unit tests.**

- `ResolverService.new(x, y).return` links to `ResolverService#return`.
- A plain `return value` statement still creates no edge.
- `Foo.new.bar` links to `Foo#bar` when `bar` is a normal method name.

**Acceptance.** `ResolverService#return` finds all three callers.

### 3. Link SitePrism page accessors to the page class

**Why it fails today.** Edges for page objects are only created when the receiver itself ends with `_page` and the file is under `step_definitions`:

```ruby
claimant_page.fill_in_...   # already handled → ClaimantPage#fill_in_...
admin_pages.responses_page  # not handled: receiver is admin_pages
```

The real call site is `admin_pages.responses_page.load` (and `.find_user`, `.change_office`, and so on). Impact on `ResponsesPage` therefore finds the class definition and no callers.

**Change.**

1. When a call looks like `something.responses_page` or `something.diversity_responses_page` (method ends with `_page` or `_section`), treat the method as a page accessor:
   - Destination: the page class `ResponsesPage` / `DiversityResponsesPage` (same camelisation as `page_symbol` already uses for the receiver).
   - Store the edge as a call to that class (construction / lookup), confidence `high` when exactly one such class exists in the repository, otherwise `low` or skip.
2. Keep the existing `foo_page.method` → `FooPage#method` rule for step definitions.
3. Limit the new rule to paths under `features/` (and possibly `spec/`), so application helpers named `*_page` are not treated as SitePrism pages.

**Unit tests.**

- `admin_pages.responses_page.load` in a step file links to `ResponsesPage`.
- `claimant_page.fill_in_name` still links to `ClaimantPage#fills_in_name` (existing behaviour).
- `user.update_page` in `app/` does not invent a `UpdatePage` class edge.

**Acceptance.** `ResponsesPage` finds both expected step files.

### 4. Soften text mention for a qualified class (safety net)

Even with edges fixed, Impact's text pass is useful when someone asks for `C100App::PaymentsFlowControl` and a comment or string only has the leaf. After items 1–3, this is optional.

**Change in `_impact_params` / `_impact_rows`.** For a target that contains `::`, also run the mention query with the leaf alone, then keep only hits that are:

- inside a file whose path or enclosing symbol already mentions the parent namespace, or
- already linked by an edge to the qualified class.

Do not open every file that mentions a short leaf like `User` or `Application`.

**Acceptance.** No drop in precision on short class names; the two c100-application files still appear even if an edge rebuild is delayed.

### 5. Sweep for related shapes (after the four misses are green)

These did not fail the current 29 cases, but the same code paths will miss them:

| Shape | Example | Approach |
|---|---|---|
| Unprefixed constant used as a type, not only `.new` | `PaymentsFlowControl` in a type comment or `rescue` | Same constant resolution as item 1; only store when the constant resolves uniquely from the caller namespace |
| `Class.method` with an unprefixed class | `OnlinePayments.create_payment` inside `C100App` | Resolve the constant side of `_CONST_CALL` with `_constant` |
| Delegate | `delegate :process, to: :queue` | Edge from the current class to the target method name when the `to:` target is a known collaborator |
| Nested SitePrism sections already partly handled | `main_content.search_field` | Leave as is unless a new eval case fails |

Only take these once the four known misses are closed and the eval set still scores 1.00.

## Delivery order

1. **Tests first** for items 1–3, written against `CallIndex` / `extract_edges` with small synthetic chunks (same style as the existing edge tests in `tests/test_rag.py`).
2. **Item 1** (constant `.new` + namespace resolution). Bump `_EDGE_VERSION` to `"9"`. Restart the app, wait for edge rebuild logs, re-run the 29 impact cases. Expect the two c100-application misses to clear.
3. **Item 2** (chained `.new(...).return`). Bump to `"10"`. Restart, re-run. Expect `ResolverService#return` to clear.
4. **Item 3** (page accessors). Bump to `"11"`. Restart, re-run. Expect `ResponsesPage` to clear. Overall recall on the 29 cases should be **1.00**.
5. **Item 4** only if a later eval case needs it.
6. Update `eval/FINDINGS.md` with the new score and a short note on what changed.
7. Commit and push.

One `_EDGE_VERSION` bump that lands all three fixes together is also fine if the tests for all three are green first. Prefer one bump if the three changes are small and reviewed together; prefer separate bumps if anything looks risky in production logs.

## Verification

Script (same shape as the earlier check):

```powershell
# After restart, confirm edges rebuilt:
# docker compose logs rag --since 5m | Select-String "call edges"

# Then score the 29 impact cases against eval/*.jsonl
python -c "..."   # or a small eval/run_impact.py if you add one
```

Pass criteria:

- Mean recall of the 29 impact expected paths is **1.00**.
- Unit tests for the three shapes pass without a database.
- Edge counts do not explode: after the rebuild, spot-check that `Time.new` / bare `return` / `user.update_page` in `app/` did not create nonsense edges.
- Existing edge unit tests still pass (inheritance, framework names, spec locality).

Optional follow-up: add `eval/run_impact.py` that reads the impact questions from the four JSONL files and prints recall the same way `run_eval.py` does for search. That stops the Impact score living only in chat history.

## Out of scope

- Changing Query defaults to the code index (recommended in `FINDINGS.md`, separate change).
- Re-ingesting repositories (edge rebuild is enough).
- Teaching Impact to invent callers that are only reached through `send`, `public_send`, or `method_missing` (those stay as `gap` edges).
- Linking every English mention of a class name in a locale file.

## Expected outcome

| After | Misses left on the 29 cases | Mean recall |
|---|---|---|
| Today | 4 targets (7 paths) | 0.91 |
| Item 1 | 2 targets (5 paths) | ~0.95 |
| Items 1–2 | 1 target (2 paths) | ~0.98 |
| Items 1–3 | 0 | **1.00** |

100% on this set is reachable with the three edge fixes above. Broader Ruby call shapes (item 5) are what keep it near 100% as the question set grows.
