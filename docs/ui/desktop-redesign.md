# Service 2 — Desktop UI redesign

## Goal

Modernize the desktop interface of Service 2 without changing the existing mobile experience or business logic.

The redesign is desktop-first from 992px and above. Mobile/tablet behavior below 992px remains unchanged unless a separate task explicitly changes it.

## Core principles

- Dark left sidebar for primary navigation.
- Compact typography and spacing so more useful information fits on screen.
- Consistent UI kit for cards, tables, filters, buttons, tabs, badges, forms and modals.
- Role-aware content: one object card, but managers/owners and service staff see different information according to permissions.
- Existing service visit history is preserved and becomes a central part of the object workflow.
- Large monitors must show more useful information instead of large empty side margins.
- Business logic and data sources remain unchanged during the visual migration.

## Adaptive workspace

Desktop layout must not use a narrow fixed Bootstrap container.

Breakpoints:

- 992–1439: compact desktop.
- 1440–1919: standard desktop.
- 1920–2559: wide desktop.
- 2560+: large workspace.

Behavior:

- Sidebar width grows slightly by breakpoint.
- Main content uses nearly all available width with controlled gutters.
- Tables gain additional visible columns on wide screens where appropriate.
- KPI grids expand from 3 to 4, 5 and 6 columns.
- Secondary blocks can move beside primary content instead of below it.
- Typography grows only slightly; larger screens are used primarily for information density.

## Desktop shell

### Sidebar

Contains role-allowed sections only.

Primary groups:
- Home
- CRM
- Calendar
- Objects
- Clients
- Finance
- Users
- Development
- Billing/renewals
- Profile

Active sections are highlighted. Sections with subnavigation can expand inline.

### Top bar

Contains:
- breadcrumb/current section
- primary page action where available
- plan badge where relevant
- notifications
- security lock
- profile

### Main content

Uses a wide adaptive workspace up to approximately 2600px on ultra-wide displays.

## UI kit

Shared desktop components:

- page header
- KPI cards
- status cards
- compact buttons
- tabs
- dense data tables
- search/filter toolbars
- badges
- alerts
- forms
- modals
- split content layouts
- wide responsive grids

## Object card — manager/owner view

Primary purpose: overview and management.

Top summary may include:
- object status
- next visit
- customer debt
- annual revenue
- open tasks
- latest document/act

Main information:
- customer
- contacts
- address
- object type
- responsible manager
- assigned service technician
- launch/conservation dates

Tabs:
- Overview
- Visits
- Equipment
- Documents
- Photos
- Finance

Overview shows a short list of recent visits with a link to full history.

## Object card — service technician view

Primary purpose: perform service work.

Financial management indicators such as customer debt and annual revenue are hidden.

Top summary may include:
- object status
- next visit
- last visit
- open tasks
- water/chemistry condition

Main information prioritizes:
- customer/contact
- address
- access instructions
- assigned technician
- equipment
- latest technical condition
- technician/manager notes relevant to the visit

Default working tab can be Visits for service users.

Tabs:
- Overview
- Visits
- Equipment
- Documents
- Photos

Finance tab is not shown without the corresponding permission.

## Visit history

The existing visit table and its data are preserved.

On desktop it becomes a first-class tab with:
- New visit action
- period filter
- technician filter
- work type filter
- all existing visit fields
- technical readings where available
- materials
- comments
- photos
- status

Wide monitors should display more columns instead of forcing horizontal scrolling.

## Migration order

1. Desktop shell and adaptive workspace.
2. Shared UI kit primitives.
3. KKM cash page.
4. Object card.
5. Object list.
6. Orders/CRM lists.
7. Finance dashboard and reports.
8. Remaining directories/settings/forms/modals.

## Safety

- No desktop redesign rule may alter mobile layout below 992px.
- No financial or service calculation changes are part of the redesign.
- Permission checks remain server-side; UI visibility follows those permissions.
- Each migration stage is delivered through CI, independent review and production deployment.
