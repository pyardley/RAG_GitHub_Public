# Plan: a second flow layout that keeps arrows from crossing

**Status (24 November 2026):** Done. The spine layout stays the default. Spread is offered when it places branches in another column and crosses less.

The spine layout is the right drawing for a short journey. Help with Fees, the staff paper journey, and a page-object journey are a single column, read from top to bottom, with a short lane when an answer leaves the page and comes back. `ministryofjustice/c100-application`, started at its opening step, is a different shape. On the indexed checkout that chart is **98 steps, all in column 0, rows 0–97, and 113 arrows: 71 straight and 42 lanes**. The longest lane covers 77 rows. The median lane covers 53. Up to 33 lanes sit side by side. There are no side arrows, because a side arrow is only used when the two boxes are already in neighbouring columns.

## Goal

- Keep the current drawing for a short journey. A chart that is already easy to read does not gain a control.
- On a chart such as C100, offer a second drawing that places each branch beside the step that leaves it, so most arrows are short.
- The main path stays in the left column, in the same order. The usual way on is still read from top to bottom.
- Choosing a label still highlights that arrow and moves to its other end.
- Prove the second drawing with a crossing count, and with the C100 chart on the page.

## Why the arrows cross

Placement and routing are split. The server decides the row, the column, and the kind of arrow. The page only turns that into SVG.

`_place` in `app/rag/flows.py` gives every step the next row in one column. A decision tree calls it with the spine first and every other step after that (`decision_chart` in `app/rag/decisionflows.py`). A step reached from near the top of C100 therefore sits near the bottom of the column. Its arrow runs the height of the chart.

`_route` then classifies each arrow:

- Next row, same column: `straight`, drawn down from the box above.
- Neighbouring columns and at most one row apart: `side`.
- Anything else: `lane`. Lanes are packed greedily. The first lane whose vertical span overlaps an existing lane is given the next channel to the right.

`drawFlow` in `app/rag/static/index.html` draws every lane in the gutter to the right of the whole column. `laneX` places a lane halfway between a tight gutter and a wide one (`formerLanePitch` is 168px per lane). A long lane that was packed first sits in an inner channel. Every shorter lane outside it has to cross that long vertical on the way out and on the way back. Return arrows use the same gutter, so they cross the forward ones as well.

The label highlight makes one of those arrows readable. It does not reduce how many of them cross.

## The second layout

Call the current drawing **Spine** and the new one **Spread**.

Spread keeps the spine in column 0 and in its present order. It moves the other steps out to the right, into the column of the branch that reached them, and on a row close to that branch. An arrow then uses the gutter between those two columns instead of one shared gutter on the far right.

### Where a step sits

1. The spine is unchanged: column 0, rows 0, 1, 2, … in the order `_spine` already chose. Do not reorder it to chase a lower crossing count. That order is the usual answer.
2. A step that is not on the spine takes its column from the step that first reaches it. A direct branch of the spine is column 1. A branch of that branch is column 2. Cap the depth (four columns is enough). Past the cap, the step stays on a lane back toward the spine.
3. Its row is the row of that source, or the next free row below it in the same column. Several answers leaving one step stack downward in that column, in label order.
4. If the next spine step is too close to leave room, insert rows rather than sending the branch to the bottom of the chart. Straight arrows on the spine still join the next spine box. The inserted rows are only the branch boxes.

Page-object charts already put one side step in column 1 through `shared` in `app/rag/pageflows.py`. The public journey puts `refund` beside `confirmation` the same way. Spread must leave those placements alone when it is not selected, and it must not invent a second column for a chart that has no long lane.

### How an arrow is drawn

Reuse `straight`, `side`, and `lane`. The page already draws those three.

- Same column, next row: `straight`.
- Neighbouring columns, at most one row apart: `side`.
- Same column, further apart, or back up the column: `lane`, in the gutter of that column.
- An arrow that crosses more than one column is a lane in each gutter it passes, one vertical run per gutter. That needs a channel on the edge (the column boundary it runs in). Today `laneX` always measures from the right-hand edge of every box. A channel lets it measure from the right-hand edge of one column.

Assign lane numbers longest span first, outermost channel first. A lane that covers many rows then sits outside the short ones, and the short horizontal stubs meet their boxes without crossing the long vertical. The current pack does the opposite: the earliest lane, which on C100 is one of the longest, is given channel 0.

Keep the join above a box that also has a straight arrow into it (`joinY` in `flowRoute`). A lane arriving at the side of that box would read as the arrow that leaves it. That rule stays.

Labels stay on the arrow, including the second copy on a long lane. Clicking or focusing a label still highlights that arrow and scrolls to the further end.

### When Spread is offered

Count crossings after both layouts are assigned. Two axis-aligned segments cross when one is horizontal, one is vertical, their ranges overlap, and they do not share an endpoint. Touching at a box does not count.

Attach `layouts.spread` only when its crossing count is lower than the spine count. The page shows **Spine** and **Spread** in `.flow-tools`, next to the zoom controls, only in that case. A short Help with Fees chart has no control.

Remember the last choice in `localStorage` under a key such as `flow-layout`. The default, and the value used when the chart has no spread layout, is Spine.

The nodes and edges on the chart stay the spine layout, so the current tests keep their rows, columns, and routes. Spread is an extra object of positions and routes, keyed by node id and by the from/to/label of each edge. The page copies those fields onto the chart and calls `drawFlow` again. Zoom, the pinned spine, and the label highlight are unchanged because they read the drawing, not the layout name.

A saved HTML file is the DOM at the time of the download, so it stores whichever drawing was on screen. The live page is the one that switches.

## Work items

Do these in order. Each item is safe to stop after: the page still draws the spine layout until the control is added.

### 1. Count crossings

Add a function beside `_route` that, given nodes and edges with row, column, route, and lane, counts crossings of the orthogonal segments those routes imply. It does not need pixel geometry. A straight arrow occupies the vertical gap between two stacked boxes. A side arrow occupies the horizontal gap. A lane occupies one horizontal stub at the source, one vertical run in its channel, and one horizontal stub at the target.

Test it on two lanes where the longer one is in the inner channel: that pair crosses. The same pair with the longer lane outside does not.

### 2. Place the branches

Add the spread placement next to `_place`. Input is the node list, the spine order, and the edges. Output is a new row and column per node id. The spine order is a new field on the chart if it is not already recoverable: today the spine is the order passed to `_place`, and off-spine steps follow it. Record `spine: true` on the spine nodes while building the decision chart, the public chart, the staff charts, and the page-object chart, so spread does not have to guess.

Test with a spine of four steps and one branch from the first step to a step that `_place` currently puts at the bottom. Spread puts that step in column 1 on the source row, or on the next row. The spine rows stay in order.

### 3. Route the gutters

Add a spread router next to `_route`. It writes `route`, `lane`, and `channel`. Longest span takes the outer lane. `drawFlow` and `flowRoute` use `channel` when it is present, and keep today's `laneX` when it is absent, so the spine drawing does not move.

Test that a branch sitting beside its source is a `side` arrow, and that two overlapping lanes in one column receive outer-first numbers.

### 4. Attach the layout

After the existing `_place` and `_route`, build the spread positions, count both, and set `layouts.spread` only when the spread count is lower. Include `crossings` on both. Leave `nodes` and `edges` in the spine layout.

Run the existing flow tests. The public journey, the staff journeys, the page-object journey, and the decision-tree journey keep their current positions. Add one test where spread is present and one where a short chart has no `layouts.spread`.

### 5. The control

In `flowView`, when `layouts.spread` is present, add Spine and Spread to `.flow-tools`. Choosing one copies the positions and routes onto the chart and replaces the SVG. The choice is stored in `localStorage`. Fit the chart again after the switch.

On the page, draw C100 both ways. Spine should still be the tall single column. Spread should show the opening column on the left and the branches in columns to its right, with short arrows into those columns. The crossing numbers on the chart should agree with the control.

### 6. The contract

Update the Flow hint so it names the control when a chart has both drawings. In `API.md`, document `layouts.spread` and `crossings`. In the `flow` tool in `mcp/server.py`, say that a large decision tree may include a second layout with fewer crossings, and that `nodes` and `edges` remain the spine drawing.

## Out of scope

- Reordering the spine. The usual answer stays the left-hand column.
- A general graph library (Graphviz, ELK, or a force-directed layout). C100 is under a hundred steps, and the saved page would have to carry the same geometry. The three route kinds already drawn are enough if the columns are right.
- Dropping the label highlight, the zoom controls, or the pinned left column.
- Changing which steps the decision tree includes. This plan only changes where those steps are drawn.

## How to know it worked

- The unit tests in item 4 pass, including the current assertions on the Help with Fees rows and on the decision-tree fixture.
- For the indexed `ministryofjustice/c100-application` chart, `layouts.spread.crossings` is lower than the spine count, and the control appears.
- A blank start path on that repository still draws **Start or continue an application**. Spine is selected until Spread is chosen.
- Choosing a label in either drawing highlights one arrow and scrolls to the other end.
- A short chart, such as the public Help with Fees journey, has no Spread control and looks as it does today.
