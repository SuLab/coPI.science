"""The one Jinja environment factory (MD-12). Every router gets its templates
object here, so a global, filter or test registered for one surface is available on
all of them and no router carries its own ``Jinja2Templates(directory="templates")``.

The admin and manager routers include the same ``_assessments_body.html`` and
``_assessment_detail_body.html`` partials, and each ``Jinja2Templates`` instance
keeps its own globals, so the registrations have to be identical on every instance.
"""

from fastapi.templating import Jinja2Templates


def make_templates() -> Jinja2Templates:
    """A new ``Jinja2Templates`` with every filter, test and global any template uses."""
    from src.services import display_format as fmt
    from src.services.assessment_chat_record import STAFF_ONLY_VERDICT_FIELDS
    from src.services.assessment_detail import key_point_sections
    from src.services.bands import band_class, band_label
    from src.services.llm import is_truncated_stop
    from src.services.prose_citations import (
        markdown_with_citation_links,
        plain_with_citation_links,
    )

    from src.web.flash import flash_context

    # `get_flashes()` for base.html's flash block (src/web/flash.py). A context
    # processor, not a global, because it closes over the request.
    templates = Jinja2Templates(directory="templates", context_processors=[flash_context])
    # `{{ dt | ts }}`: one UTC datetime rendering (display_format.timestamp), a filter
    # so a template can never print a raw `datetime.__str__`.
    templates.env.filters["ts"] = fmt.timestamp
    # A Jinja TEST so `admin/llm_calls.html` can `selectattr('stop_reason',
    # 'truncated_stop')` and reach the real predicate (src/services/llm.py), the
    # single definition of "stopped before it finished". A test, not a filter or a
    # global: `selectattr` takes a test name.
    templates.env.tests["truncated_stop"] = is_truncated_stop
    # Key-point sections of a stored `key_points` value (current or legacy labels).
    # A global rather than a context key: the admin assessments handler forbids a new
    # context key (see the comment on `_assessments_body.html`'s card-list block).
    templates.env.globals["key_point_sections"] = key_point_sections
    # Render-time URL -> "cited paper" rewriting (spec 2026-09-21 §7).
    templates.env.globals["md_citations"] = markdown_with_citation_links
    templates.env.globals["plain_citations"] = plain_with_citation_links
    # The verdict fields only staff see (VERDICT_FIELDS, `staff_only`); the template
    # gates the hub's own bullets on it, as the chat record does (S2-05).
    templates.env.globals["staff_only_verdict_fields"] = STAFF_ONLY_VERDICT_FIELDS
    templates.env.globals["band_class"] = band_class
    templates.env.globals["band_label"] = band_label
    return templates
