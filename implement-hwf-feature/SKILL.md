---
name: implement-hwf-feature
description: >-
  Implement or amend Cucumber step definitions, SitePrism page objects, and
  support helpers for the HMCTS Help with Fees public application
  (hmcts/hwf-publicapp), the staff application (hmcts/hwf-staffapp), and the
  C100 child arrangements application (ministryofjustice/c100-application).
  Use when a tester supplies a feature file under features/ (for example
  features/address/address_married.feature, features/sign_in.feature, or
  features/applicant_details.feature) and an environment URL and needs those
  steps turned into the button presses, radio choices, and text fields the
  page actually shows. Use when the user mentions implementing, writing, or
  fixing a feature file, step definition, page object, Capybara locator, or
  Cucumber scenario for Help with Fees, hwf-publicapp, hwf-staffapp, C100,
  c100-application, or files under features/step_definitions,
  features/support, or features/support/page_objects.
---

# Implement a Help with Fees feature

Work in the checkout for the suite you are changing. The feature file states the journey. The environment URL shows the live copy. Read the patterns file for that suite before adding or changing a page object:

- `hmcts/hwf-publicapp`: [references/hwf-publicapp.md](references/hwf-publicapp.md). Helpers are `features/support/generic_helpers.rb` and `features/support/navigation_helper.rb`. The advance button is `continue`.
- `hmcts/hwf-staffapp`: [references/hwf-staffapp.md](references/hwf-staffapp.md). Helpers are `features/support/helpers/generic_helpers.rb` and `features/support/helpers/navigation_helpers.rb`. The advance button is that page's `click_next`.
- `ministryofjustice/c100-application`: [references/c100-application.md](references/c100-application.md). Helpers are `features/support/generic_helpers.rb`. The advance button is `click_continue_button` on `BasePage`.

The steps below describe the public app. Where the staff file or the C100 file differs, follow that file.

Leave `features/support/env.rb` alone. Cucumber loads every `features/**/*.rb` file, and that file says it is generated.

## What you are given

- A feature path, such as `features/address/address_married.feature`
- An environment URL, such as `https://hwf-publicapp.aat.platform.hmcts.net/`

Ask for whichever is missing before writing steps. The URL is evidence of the visible text. The suite still runs against the local Rails app unless the user asks to drive that host.

## Order of work

1. Read the whole feature, including the Background.
2. Search `features/step_definitions` for each step phrase. Reuse a definition that matches the whole phrase. `I enter my address` and `I enter my address with postcode` are different steps. A new regex that is only a prefix of an existing one will run the wrong code.
3. Read the question view, `config/locales/en.yml`, and the live page before choosing a locator. The three have to agree. If they disagree, the live page is what the tester sees and the locale is what the local suite renders. Report the mismatch instead of picking one silently.
4. Add only the missing step, page method, accessor, or navigation helper.
5. Run that scenario.

`And` and `But` match the same definitions as the step text they repeat. Do not add a second definition for `And I click continue` when `When I click continue` already exists.

## Where a step goes

| Step kind | Where the behaviour lives |
| --- | --- |
| Given that opens a question | A `to_<page>` method in `navigation_helper.rb`, called from the step. Build it by calling the earlier pages' submit methods. Pass `'married'` or `'single'` when the feature says so. |
| When that fills or clicks | A method on the page object. Keep the step as one call. |
| Then that checks the next page | `expect(<page>_page).to be_displayed` in the step for that destination. `be_displayed` compares the browser path with `set_url`. |
| Then that checks an error the common step already covers | No new step. `Then I should see 'Enter your postcode' error message` is in `common_steps.rb`. |
| Timeout ("after a long time") | `travel 61.minutes` around the same actions. The session expires after 60 minutes. Include `ActiveSupport::Testing::TimeHelpers` on the page. `env.rb` calls `travel_back` after the scenario. |

Name the step file after the question: `features/step_definitions/address_steps.rb` for `features/address/*.feature`. Name the page `AddressPage` in `features/support/page_objects/address_page.rb`. Add `def address_page` in `generic_helpers.rb` only when the class is new. A new file in `page_objects/` is loaded by the glob in `env.rb`.

Match the style of the file you are editing. Existing page methods often call the helper (`address_page.street(...)`, `apply_type_page.content.paper.click`) rather than `self`.

## SitePrism

Every page object is a SitePrism page. `BasePage` subclasses `SitePrism::Page`. Declare the path with `set_url`, the region with `section :content`, and each heading, caption, radio, or checkbox with `element`. Click and assert through that section.

`features/support/page_objects/apply_type_page.rb` is the choice-page pattern:

```ruby
class ApplyTypePage < BasePage
  set_url '/applications/{app_id}/questions/apply_type'

  section :content, '#content' do
    element :step_info, '.govuk-caption-l', text: 'Step 20 of 22'
    element :header, 'h1', text: 'Will you be making your court or tribunal application on paper or using an online service?'
    element :paper, 'label', text: 'Paper'
    element :online, 'label', text: 'Online service'
  end

  def applying_by_paper
    apply_type_page.content.paper.click
    continue
  end

  def applying_by_online_service
    apply_type_page.content.online.click
    continue
  end
end
```

`content.paper` is the SitePrism element for the Paper label. `.click` presses it. `have_header` and `have_paper` are the assertions for those elements. `expect(apply_type_page).to be_displayed` uses `set_url`. `navigation_helper.rb` reaches this page with `apply_type_page.applying_by_paper` and `apply_type_page.applying_by_online_service`.

Use SitePrism elements for radios, checkboxes, headings, and captions. Text fields in this suite are often `fill_in` with the label, as on `AddressPage`. A new choice should be an `element` inside the section, not a new `find('label', ...)`. `common_steps.rb` still has one raw find for "Online service"; call `apply_type_page.applying_by_online_service` instead of copying that find.

## Copy the visible text from the app

Question pages are rendered by `app/views/questions/edit.html.slim`.

- The h1 is `questions.<form_id>.text` in `config/locales/en.yml`. The step caption is `questions.<form_id>.breadcrumb` (`Step %{page_number} of 25` on the current questions). `page_number` is the index of that symbol in `Settings.navigation` (`config/settings.yml`) plus one. Skipped questions are still in that list. Page objects hard-code the rendered caption, and those strings differ: `AddressPage` says `Step 21 of 25`, `ApplyTypePage` says `Step 20 of 22`. Read the caption on the live page and use that exact string.
- Field labels are `activemodel.attributes.forms/<form>.<field>`. The slim label uses that string. `fill_in` that string. Street is `House number and street`, not `street`.
- Error sentences are `activemodel.errors.models.forms/<form>.attributes` in `en.yml`. The feature quotes the English sentence. The shared Then step looks for it in the error summary and in `.govuk-error-message`.
- Continue is `t('submit_button')`, which is `Continue`, on `input[type="submit"]`. Click it with the shared `continue` method. The checklist uses `checklist_continue` because that button is a link-styled button, not the submit input.

Question URLs are `/applications/{app_id}/questions/<form_id>`. `set_url` uses that pattern, with `{app_id}` left for SitePrism. The form id is the symbol in `Settings.navigation`, such as `applicant_address`.

The homepage is not a question. Its section root is `#main-content`. Question pages use `#content`.

Open the environment URL to confirm the heading, caption, button, and whether fields are behind address lookup. Question pages are reached after Start now and the earlier answers, not from the site root. Walk that far. If the environment cannot be clicked, say so and use the view plus the locale.

## Address lookup and a deployed host

`disable_address_lookup` stubs the Ordnance Survey token request so the manual street, town, and postcode fields render. Call it from a Given that reaches the address page. On a host where lookup is really shown, the fields sit behind **Enter address manually**. `AddressPage#submit_full_address_demo` clicks that link first. `navigation_helper.rb` already chooses the demo method when `Capybara.app_host` is `https://public.demo.hwf.dsd.io`. For any other deployed host, look at the page and use the manual-entry method when the fields are not there yet.

## Run it

From the app checkout, one scenario:

```bash
bundle exec cucumber features/address/address_married.feature:6
```

The number is the scenario's first line. Local runs use rack_test and do not open a browser. Scenarios tagged `@javascript` use headless Chrome.

When the user wants that scenario driven at the given URL:

```bash
CAPYBARA_APP_HOST=https://hwf-publicapp.aat.platform.hmcts.net bundle exec cucumber features/address/address_married.feature:6
```

A host other than localhost selects Firefox. WebMock allows that host and blocks everything else, so Ordnance Survey and the submission API stay stubbed. Confirm the browser host has no `:3000` suffix. This suite sets `Capybara.always_include_port = true`, and a port on the deployed host means the run never reached it.

## Done when

- Every step in the feature matches one existing or new definition, and no definition is a prefix of another.
- Clicks and fills go through the page object. Continue goes through `continue`.
- Headings, labels, and errors match the locale and the live page.
- `bundle exec cucumber` on the scenario exits 0, or the failure is a real product mismatch. Report that mismatch. Do not loosen the step to make it pass.
