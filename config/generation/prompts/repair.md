Repair the complete file using only the concrete findings below.

FILE PATH: {target_path}
CURRENT CONTENTS:
{current_contents}
BUILD ERRORS:
{build_errors}
CONTRACTS:
{contracts}
NAMESPACE / IMPORT MAP:
{namespace_map}
PINNED-VERSION API REFERENCES:
{api_reference_snippets}

Preserve correct behavior and names. Fix every finding without changing authoritative
contracts. Imports must resolve through the map; an unmapped symbol must be removed, not
invented. Return a complete replacement file with no TODO, stub, placeholder, truncation,
Markdown fence, or explanation.
