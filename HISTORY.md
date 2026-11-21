# History of changes

Code RAG answers questions about indexed GitHub repositories. The index is Postgres with pgvector, the app runs in Docker, and the page is at http://127.0.0.1:8080. This is the history of `main` from the first commit on 5 November 2026 through 20 November 2026: 64 commits.

The work falls into six stretches: a searchable index and a page, sharper lookup of Cucumber and directories, an impact graph, embeddings and an evaluation, journey charts, then Features, Testing, and the demo.

## 5–6 November: an index, a page, and a feature skill

`cd27fb6` starts the project as a portable Docker setup. The same machine can index a GitHub repository and answer a code question or an impact question from Postgres.

`2df32da` adds the page. It switches between query, impact, and search, and each passage opens the indexed lines on GitHub. `38fe54b` shows how many files have been read while a long index is still running. `acb74f7` names the repository's default branch in the branch hint, because an empty branch field uses that default.

Questions about Cucumber start to follow the code rather than stopping at the feature file. `e7c59e3` retrieves the step definition and the page-object methods that fill fields and click buttons. `d0ae50d` keeps a "list the features" question inside feature files. A question about how one named feature runs still follows the step.

`af10908` plans an ordinary question with a small model before searching. The planner may use only words from the question. A separate model writes the answer. Feature lists and named feature files are still searched as written. `1edd6f8` records planning, embedding, search, and answer time, and the tokens used, in a Resources footer.

`6f87431` changes a question such as "look under /app for 4250". It had been matching the whole sentence and returning specs. It now looks for those values inside the named directory. `139aef8` covers that lookup, and the usage footer, in the unit tests.

The same two days add a skill for writing Help with Fees features (`79ad7c6`). `e7fb810` splits the public app (its continue button and helpers) from the staff app (Next, and its own helpers). `4a7e7af` records the C100 Continue button, hidden yes/no inputs, and Developer Tools bypass. `eff1f49` records the employment tribunal locale-key locators and its Save and continue or next button.

`d538705` adds a table of contents to the README.

## 6–7 November: quoted text, patterns, and ranking

Quoted search is tightened in a short run of fixes.

`2a3273b` searches a named directory for a quoted phrase, so a Cucumber step in quotes finds the step definition and not only the feature line. `b3ca175` treats a step pattern as a match when the quoted words sit inside a regular expression. `d15f857` lists every line that contains the quoted text, including the step definition the answer had been leaving out.

`c4395c8` treats regex syntax inside quotes as a pattern, so `[0-9]*` matches any digits. `195fc65` keeps a quoted phrase or pattern on that lookup when nothing matches, instead of falling through to a general search, and the page shows when the process started. `146d22c` shows the matching lines and follows a quoted step into the step definition and page objects. A pattern Postgres rejects is reported as a message.

`2e9e649` ranks passages by closeness, leaves ignored folders out, and says when more matches exist than the Passages limit. `3951418` keeps a search for "limit" returning a passage about a threshold. `ad1fcbd` writes each search to the container log so a surprising passage can be traced.

## 9–11 November: what a change affects

Impact becomes a call graph rather than a text search.

`dc0c815` can drop one repository's index without touching the others. RSpec examples, SitePrism sections, and FactoryBot traits are stored as their own passages. `2551225` walks direct calls, factories, page elements, aliases, and Cucumber steps for a few hops and shows that path. `send`, `public_send`, `define_method`, and `method_missing` stay unresolved, with no guessed target.

`539a3d8` resolves `FactoryBot.create(:claim)` to the factory, including `build` and `create_list`, so the caller walk can start. The page says when callers are left out. `7ec25ed` draws those paths. Arrows point from a caller toward the definition, and choosing a box opens that passage.

Bare calls are the hard part. `68e8a06` links a helper called by name when only one indexed method has that name. `a30175c` marks a caller as possible when two to five methods share the name, and draws that hop as a dashed arrow. `a67e018` reads bare calls from the whole line, not only from the start of a line, and links a name only where Ruby could reach it: the caller's own class, a parent, a module, or a method outside any class. Spec methods stay in their file. Application code is not linked to test helpers. A lower-case name no longer matches a class, and factory attributes are not calls.

`cd1dfa0` collapses a large diagram. Past 12 boxes, three or more callers in the same file become one box with a count. Choosing it lists them. Expand all opens every group.

Around that, `3d60725` asks before an index is removed, after a syntax error had stopped the repository list loading. `4287976` fills the branch field from the remote branches. `b84b010` compares each indexed commit with the branch on GitHub, marks a branch that has moved on, and offers Update the index. `b1e3586` renders the written answer as Markdown without setting HTML. `f24d51b` adds an MCP server so another model can query, search, and run impact through the web API. `df3cc70` adds `.mcp.json` for Claude Code, and `8de4fa8` adds a branches tool. `d202d11` and `8b3cb6f` document the Help with Fees queries and the et-full-system claim factory. `32a0be2` notes that a `.env` change needs `docker compose up -d rag`, because a restart keeps the old settings.

`0828f82` runs the unit tests on GitHub Actions. `8800dec` makes the invalid-pattern test and the MCP stdio test pass without a local database and on Python 3.12.

## 16–17 November: two indexes, and a measured Impact

`1b6fb21` saves a result as one HTML file, including the relationship diagram, because Markdown cannot open and close the grouped boxes.

`e6416a3` stores two embeddings for every chunk: a 384-dimension text index and a 768-dimension code index, each with its own HNSW index. A query can use text, code, or both, and can fold in keyword rank by raw score or by reciprocal rank fusion.

`d5023cf` is the practical limit of the code model. Embedding 32 chunks of up to 8192 tokens needed more than 6 GB and stopped Docker Desktop. The code model now embeds 4 chunks at a time and reads at most 2048 tokens of each. Schema setup runs once per process, so status requests no longer deadlock. Only one ingest can run, and every open page disables the index buttons while it does. The same commit adds `eval/`: 95 checked questions across hwf-staffapp, hwf-publicapp, et-full-system, and c100-application, plus `FINDINGS.md`.

Impact is then scored and raised. `5db2b2a` plans the path from 91% caller recall toward 100%. `96a8cd4` links `Const.new`, chained `Const.new(...).method`, and SitePrism page accessors, and lands at 100% on that set. `5c4939d` makes code plus reciprocal rank fusion the default Query settings. `bd8a454` sends a "what calls this" question to Impact and tightens Query ranking. `2cd73f7` states that Recall@8 and MRR are scores from 0 to 1, with higher better.

## 18 November: journeys

`d7b5c07` fixes an Impact crash when the related search follows class definitions.

Flow starts with the Help with Fees question journeys (`b0b0af7`). Each box opens that step on the indexed commit. `986d034` draws the ET1 and ET3 claim and response from page objects, so Flow is not only Help with Fees. `69c7189` takes a start path such as `/steps/opening/start_or_continue` and draws the decision tree from there, which is how the C100 journey is drawn.

`5ce135a` lets a question follow the arrows on the chart, marks a step that a feature or step definition names, and shows what the branch has changed since the index. `1a3d558` separates labels that sat on top of each other, and scrolls to the other end of an arrow when its label is chosen. `ab0a1a4` lists steps nothing visits, and scrolls to that box when a name is chosen.

`dd5ac74` stops the checkout walk treating `/tmp` as a skipped folder. `7dd5323` adds the first demo script and a subtitled recording of Query, Impact, Search, and Flow.

## 19–20 November: Features, Testing, and a chart that fits

`1c73f79` adds Features. A pasted Gherkin line is matched under `features/`. A request to write a feature is searched as the behaviour, and the written answer quotes only steps that appear in the passages. A check with no such step is named and left unwritten. `de7f352` treats Given, When, Then, And, and But as the same step. `cef9674` lists files under `features/` before other directories. `e93260d` records the scores after that change.

`a53c035` adds Testing. It looks up the Cucumber commands the repository documents, including `cucumber.yml`, and leaves RSpec and Minitest out. The answer quotes those commands for the suite, a group, one scenario, and debugging a scenario that is being written.

`9e3f5b5` turns Query, Impact, Search, Flow, Features, and Testing into tabs. `2b05a00` redraws flow charts so they fit the frame: narrower boxes, return arrows beside the steps, zoom, and the main column pinned when the chart is scrolled sideways. `0cf6e16` replaces the demo with one that includes Features, Testing, and opening GitHub from an Impact reference and from a journey box.

`56961ab` fixes the command Testing gives for a named feature file. `dob_married.feature` is stored as `features/dob/dob_married.feature`. The answer had been dropping the `dob` directory, and on one machine it also copied a doubled `cucumber` from the features README. The command is now `bundle exec cucumber` once, followed by the indexed path.
