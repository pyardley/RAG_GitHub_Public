# hwf-staffapp patterns

Taken from `hmcts/hwf-staffapp`. The stack is the same Cucumber, Capybara, and SitePrism shape as the public app. The names, the button, and the journey are not. Follow this file when the checkout is the staff app.

## Where the files live

| Piece | Path |
| --- | --- |
| Features | `features/*.feature` (flat files, such as `features/sign_in.feature`) |
| Step definitions | `features/step_definitions/` |
| Page objects | `features/support/page_objects/` |
| Page accessors and sign-in | `features/support/helpers/generic_helpers.rb` |
| Journeys | `features/support/helpers/navigation_helpers.rb` |

Leave `features/support/env.rb` alone. Cucumber loads every `features/**/*.rb` file.

Page accessors are memoised in `generic_helpers.rb`:

```ruby
def sign_in_page
  @sign_in_page ||= SignInPage.new
end
```

Add a new `def <name>_page` there only when the page class is new. A new file in `page_objects/` is loaded with the others.

## How a scenario starts

Staff scenarios sign in, then process an application. They do not walk the public applicant journey and they do not take a married or single argument.

`features/sign_in.feature`:

```gherkin
Background: Navigating to the sign in page
  Given I am on the Help with Fees staff application home page
  When I am not signed in
  Then I am redirected to the sign in page

Scenario: Successful sign in as a user
  When I successfully sign in as a user
  Then I am taken to my user dashboard
```

`sign_in_steps.rb` calls `dashboard_page.load_page` for the home page, asserts `sign_in_page.content` with `have_sign_in_title`, and signs in through `sign_in_as_user` (and `sign_in_as_admin`, `sign_in_as_manager`, and the other helpers already in `generic_helpers.rb`). `SignInPage` uses `set_url '/users/sign_in'`. Reuse those helpers. Do not fill the email and password fields from a new step when one of them already signs in the role the feature names.

A longer journey is a `go_to_*` method in `navigation_helpers.rb`. `go_to_paper_evidence_page` calls `personal_details_page.submit_all_personal_details_ni_with_no_answer_for_benefits`, `application_details_page.submit_fee_600`, `savings_investments_page.submit_less_than_ucd`, and `benefits_page.submit_benefits_yes`, expecting `have_header` on the page it lands on. Extend the nearest `go_to_*` method. Do not re-click the journey from sign-in in a new step.

## The advance button is Next

Each page defines its own `click_next`. There is no shared `continue`. `BenefitsPage` is the choice-page pattern:

```ruby
class BenefitsPage < BasePage
  set_url_matcher %r{/applications/[0-9]+/incomes}

  section :content, '#content' do
    element :header, 'h1', text: 'Benefits the applicant is receiving'
    element :benefit_question, '.govuk-label', text: 'Is the applicant receiving one of these benefits?'
    element :no, 'label', text: 'No', visible: false
    element :yes, 'label', text: 'Yes', visible: false
    element :next, 'input[value="Next"]'
  end

  def submit_benefits_yes
    content.wait_until_yes_visible
    content.yes.click
    click_next
  end

  def click_next
    content.wait_until_next_visible
    content.next.click
  end
end
```

Click the label. The radio is often `visible: false`, so the method waits with `wait_until_yes_visible` before `content.yes.click`. A new choice page should follow that, including the wait, and then call that page's `click_next`.

Text fields use the visible label. `PersonalDetailsPage#submit_required_personal_details` does `fill_in 'First and middle names'` and `fill_in 'Last name'`, clicks `content.status_single`, then `click_next`.

## URLs

`BasePage < SitePrism::Page`. Question-style pages use `set_url` or `set_url_matcher`. `SignInPage` uses `set_url '/users/sign_in'`. `BenefitsPage` uses `set_url_matcher %r{/applications/[0-9]+/incomes}` because the id is a number, not the public app's `{app_id}` token.

Some pages set no URL, including `evidence_page.rb`, `find_application_page.rb`, and `confirmation_page.rb`. They are reached by a click from the previous page. Give a new page a `set_url` or `set_url_matcher` only when the path is stable. Otherwise reach it the way the nearest `go_to_*` method already does, and assert with `have_header` rather than `be_displayed`.

Copy the h1 from the page object that already covers that screen, or from the live page. Do not import the public app's `Step N of 25` captions. Staff pages in this suite generally do not declare a step caption.

## Run it

From the staff app checkout:

```bash
bundle exec cucumber features/sign_in.feature:8
```

The number is the scenario's first line. Local runs use the drivers named in `features/README.md` (rack, and headless Chrome for the JavaScript driver).
