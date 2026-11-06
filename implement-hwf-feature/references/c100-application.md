# c100-application patterns

Taken from `ministryofjustice/c100-application`. The stack is the same Cucumber, Capybara, and SitePrism shape. The paths, the Continue button, and the way a scenario walks the form are this suite's own. Follow this file when the checkout is the C100 application.

## Where the files live

| Piece | Path |
| --- | --- |
| Features | `features/*.feature` (flat files, such as `features/applicant_details.feature`) |
| Step definitions | `features/step_definitions/` |
| Page objects | `features/support/page_objects/`, grouped in subfolders such as `applicant/`, `children/`, `miam/`, `opening/` |
| Page accessors | `features/support/generic_helpers.rb` |
| Shared clicks, visits, and journeys | `features/step_definitions/common.rb` |
| Shared fills, radios, and page-object steps | `features/step_definitions/forms.rb` |

There is no navigation helper. A journey is a step in `common.rb` or in the step file for that part of the form.

`features/support/env.rb` is the cucumber-rails generated file, and this suite has already registered the Cuprite driver and the page-object requires in it. Leave that driver registration as it is. Cucumber loads every `features/**/*.rb` file. The `Dir` glob in `env.rb` loads every file under `page_objects/`. `base_page.rb` and `yes_no_page.rb` are required before that glob so subclasses can inherit them.

Page accessors are memoised in `generic_helpers.rb`:

```ruby
def applicant_names_page
  @applicant_names_page ||= ApplicantNamesPage.new
end
```

Add a new `def <name>_page` there only when the page class is new. Put the class in the subfolder for that part of the journey, for example `features/support/page_objects/applicant/applicant_names_page.rb`.

## How a scenario starts

Scenarios open the form through the Developer Tools bypass, then visit a step path. They do not take a married or single argument, and they do not sign in.

`features/applicant_details.feature`:

```gherkin
Feature: Add an applicant to the application
  Background:
    Given I have started an application
    And I have entered a child with first name "John" and last name "Doe Junior"
    Then I visit "steps/applicant/names"

  @happy_path
  Scenario: Applicant personal details
    Then I should see "Enter your name"
    When applicant names page I submit names "John" and "Doe Senior"
    Then I should see "Do the other people named in this application (the respondents) know any of your contact details?"
```

`I have started an application` in `common.rb` visits `/`, opens the **Developer Tools** summary, and clicks **Bypass postcode**. It expects `/steps/opening/consent_order`. `I have completed an application` clicks **Bypass to CYA**. Reuse those steps.

`I have entered a child with first name "..." and last name "..."` visits `/steps/children/names` and calls `children_names_page.add_child`. `I visit "steps/applicant/names"` is the shared visit step.

A longer journey is a step that calls page methods in order. `I complete the applicant details journey` in `common.rb` calls `applicant_names_page.submit_names`, the privacy and refuge submits, `applicant_personal_details_page.submit_personal_details`, `applicant_relationship_page.submit_relationship`, `address_lookup_page.click_outside_uk`, then the address and contact submits. Extend the nearest journey step.

## The advance button is Continue

`BasePage` owns the button and the link. Page methods call these. There is no per-page `click_next` and no `continue` helper in `generic_helpers.rb`.

```ruby
element :continue_button, 'button', text: 'Continue'
element :continue_link, 'a', text: 'Continue'

def click_continue_button
  continue_button.click
end

def click_continue_link
  continue_link.click
end
```

Most submits end with `click_continue_button`. The privacy summary and the MIAM page advance with `click_continue_link`, because that screen shows an anchor. Match the control on the page you are changing.

Scenarios also press the button through the shared step `I click the "Continue" button` in `common.rb`, which uses `find_button`. Keep that step when the scenario is provoking a validation error. A new page method that submits a valid answer calls `click_continue_button`.

`I should see the save draft button` asserts the button **Save and come back later**.

## Yes/no pages

A yes/no screen subclasses `YesNoPage` and adds the URL and the heading. `YesNoPage` clicks the hidden radio input, then Continue:

```ruby
class YesNoPage < BasePage
  section :selection_area, '#main-content' do
    element :answer_yes, "input[value='yes']", visible: false
    element :answer_no, "input[value='no']", visible: false
  end

  def submit_yes
    selection_area.answer_yes.click
    click_continue_button
  end

  def submit_no
    selection_area.answer_no.click
    click_continue_button
  end

  def submit(answer)
    if answer == true || answer == 'yes'
      submit_yes
    else
      submit_no
    end
  end
end

class ApplicantRefugePage < YesNoPage
  set_url '/steps/applicant/refuge'

  section :content, '#main-content' do
    element :header, 'h1', text: 'Are you currently resident in a refuge?'
  end
end
```

`ApplicantRefugePage`, `HasOtherChildrenPage`, `UrgentHearingPage`, and the other yes/no screens follow that. A new yes/no page subclasses `YesNoPage`, sets `set_url` and the `h1`, and the step calls `submit('yes')` or `submit('no')`.

`ApplicantPersonalDetailsPage` is the pattern when the page has several radios. Each input is an element with `visible: false`, and the method clicks it:

```ruby
element :gender_male, "input#steps-applicant-personal-details-form-gender-male-field", visible: false

content.gender_male.click
```

Feature steps that click one option by its caption use the label. `I click the radio button "I can provide an email address"` in `forms.rb` does `find('label', text: text).click`. Keep that for a scenario that selects a single option and then presses Continue itself. `I choose "Yes"` clicks the label and then `find_button('Continue')`.

## Text fields

A page method sets the input by its `name`, then clicks Continue. `ApplicantNamesPage`:

```ruby
class ApplicantNamesPage < BasePage
  set_url '/steps/applicant/names'

  section :content, '#main-content' do
    element :header, 'h1', text: 'Enter your name'
    element :first_name_field, "input[name='steps_applicant_names_split_form[new_first_name]']"
    element :last_name_field, "input[name='steps_applicant_names_split_form[new_last_name]']"
  end

  def submit_names(first_name, last_name)
    content.first_name_field.set first_name
    content.last_name_field.set last_name
    click_continue_button
  end

  def continue_without_filling
    click_continue_button
  end
end
```

The content root is `#main-content`. Copy the `name` from the existing page or from the form. Date fields use the Rails multipart names, as `ApplicantPersonalDetailsPage` does for `dob(3i)`, `dob(2i)`, and `dob(1i)`.

Scenarios that fill one field use the visible label through `I fill in "First name(s)" with "!@£"`. That step is `fill_in(field, with: value)` in `forms.rb`. Use it when the scenario is showing a validation error. Use `.set` on the named input when the page method submits the whole answer.

`continue_without_filling` clicks Continue on an empty form so the scenario can assert the error links.

## How a step names the page

`forms.rb` binds the feature phrase to the accessor. The phrase names the page:

```ruby
When(/^applicant names page I submit names "([^"]*)" and "([^"]*)"$/) do |first_name, last_name|
  applicant_names_page.submit_names(first_name, last_name)
end
```

The feature line is `When applicant names page I submit names "John" and "Doe Senior"`. A new page method gets a step in that shape, in `forms.rb` or in the step file for that journey (`applicant_steps.rb`, `children_steps.rb`, and the other files already there). Values the feature quotes stay in the capture groups.

These shared steps already exist in `common.rb`. Reuse them:

| Step | What it checks |
| --- | --- |
| `I should see "..."` | The text is on the page |
| `I should see a "Enter the first name" link to "#steps-applicant-names-split-form-new-first-name-field-error"` | The error-summary link and its href |
| `Page has title "Error: Applicant names - Apply to court about child arrangements - GOV.UK"` | The document title. Error pages prefix `Error: ` |
| `I should be taken to the applicant details page` | `expect(applicant_names_page).to be_displayed` and `have_header` |

## Address

`AddressLookupPage` sets the postcode input or clicks the link **I live outside the UK**:

```ruby
def click_outside_uk
  content.outside_uk_link.click
end
```

Applicant journeys call `address_lookup_page.click_outside_uk`, then `applicant_address_details_page.submit_address_details` with the building, town, country, and the five-year answer. That skips the lookup API. `submit_postcode` is there for a scenario that does look the postcode up, and `common.rb` stubs courtfinder around those clicks.

## Timeouts

`I wait and click the "Continue" button` in `common.rb` moves time with `travel_to 61.minutes.from_now` and then clicks. The expected text is `Sorry, you'll have to start again`. `env.rb` calls `travel_back` after each scenario. `BasePage` already includes `ActiveSupport::Testing::TimeHelpers`.

## URLs and headings

`set_url '/steps/applicant/names'`. Pages whose path includes a record id use `set_url_matcher`, for example `%r{/steps/children/orders/.*}` on `ChildOrdersPage`. Give a new page a `set_url` when the path is stable, and a matcher when the path has an id.

Headings are the `h1` text inside `#main-content`. Some are a prefix because the name is interpolated: `Enter your name`, `Provide details for`, `Address of`. Copy the stable part from the page object that already covers that screen, or from the live page. This suite does not use `Step N of 25` captions.

`expect(applicant_names_page).to be_displayed` compares the browser path with `set_url`.

## Run it

From the C100 checkout. Cucumber drives the Rails app in-process with Cuprite. The browser stays headless unless `SHOW_BROWSER` is set. This suite has no `CAPYBARA_APP_HOST` switch.

```bash
bundle exec cucumber features/applicant_details.feature
bundle exec cucumber features/miam.feature -t @happy_path
```

A line number selects one scenario: `bundle exec cucumber features/applicant_details.feature:8`, where `8` is that scenario's first line. `@happy_path` and `@unhappy_path` are the tags the README uses.
