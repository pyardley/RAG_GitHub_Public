# Demo script

A short recording of the web app at http://127.0.0.1:8080. Each beat is one action, then the subtitle that stays on screen while that action is visible. Nothing is recorded until this script is approved.

Rough length: four minutes. The app is already running and the four repositories are already indexed. The recording does not start an ingest.

## Before recording

- Browser window only. No editor, terminal, or `.env`.
- Repository list visible, including Help with Fees public and the C100 application.
- Query is selected, with Code, Reciprocal rank, 12 passages, and Written answer on.

## Beats

### 1. Open

Show the page heading and the four tasks: Query, Impact, Search, Flow.

Subtitle: Code RAG answers questions from the code of an indexed GitHub repository.

### 2. Choose a repository

Open the repository list. Pause on Help with Fees public, then on a repository marked "branch has newer commits".

Subtitle: Each indexed repository is one commit. A branch that has moved on is marked, and Update the index brings the index forward.

### 3. Ask a question

Select hmcts/hwf-publicapp. Leave Query selected. Type `where is the application submitted` and choose Ask. Wait until the written answer and the passages are on screen. Point at the index line (Code, reciprocal rank fusion) and at one passage path.

Subtitle: Ask in your own words. The closest passages come back from the code index, with a short written answer.

### 4. Open the code

Choose the first passage link. Let the GitHub page open on the indexed commit, at those lines. Return to the app.

Subtitle: Each passage opens those lines on the commit that was indexed.

### 5. Match the words exactly

Back on Query, replace the question with `look under /features for "with income '[0-9]*' and average"` and choose Ask. Show a passage that contains that step.

Subtitle: Quote the text to match it. Inside the quotes, `[0-9]*` matches any amount, and the search stays under features.

### 6. See what would change

Choose Impact. Type `OnlineApplication` and run it. Show the groups of definitions and references, then the relationship diagram. Choose one caller box so its passage appears.

Subtitle: Impact shows where a class is defined and what calls it. An arrow points from a caller toward the definition.

### 7. Search every index

Choose Search. Clear the repository selection so the search covers every indexed repository. Type `look for limit` and run it. Show passages from more than one repository, if both appear.

Subtitle: Search asks every indexed repository, then sorts the passages by how close they are.

### 8. Draw a journey

Choose Flow. Select ministryofjustice/c100-application. The start path is `/steps/opening/start_or_continue`. Choose Show. When the chart appears, pause on the note under the title.

Subtitle: Flow draws the journey from the decision tree, starting at this step. A corner mark means a feature or a step definition names that step.

### 9. Steps nothing visits

Open "steps with no feature or step definition". Choose Research consent. Wait until that box is in the centre of the chart, and show that it has no corner mark.

Subtitle: The list is every box with no corner mark. Choosing a name moves the page to that box.

### 10. Follow an arrow

Scroll back to the top of the chart. Choose the "No court" label beside the first box. Wait until "Sorry, you cannot apply online" is centred. Choose the "No court" label beside that box, and wait until the first box returns.

Subtitle: Choose a label to move to the other end of that arrow. The label at that end brings you back.

### 11. Ask the journey

In "Ask about this journey", type `What happens if the applicant says no to a MIAM?` and choose Ask. Show the line "Taken from the arrows on this journey" and the link "MIAM exemption claim".

Subtitle: A question that names a step and an answer follows that arrow, and the link opens the step.

### 12. Keep the result

Scroll up and choose Save as HTML. Show the file saving. Do not open the file unless the download is already in view.

Subtitle: Save as HTML keeps this result in one file on your machine. The app does not keep a copy.

### 13. Close

Return to the repository list. Leave "Index a repository" visible and do not choose it.

Subtitle: A new GitHub repository can be indexed from this page. The first index takes several minutes.
