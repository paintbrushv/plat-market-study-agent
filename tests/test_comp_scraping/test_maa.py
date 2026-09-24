from __future__ import annotations

from etl.comp_scraping.engines.maa import parse_maa_available_units


def test_maa_parser_extracts_rendered_available_unit_cards() -> None:
    html = """
    <main>
      <h1>MAA Highlands North</h1>
      <section class="property-available-apartments">
        <article>
          <h2>Unit #013107</h2>
          <p>2 Beds, 1 Baths</p>
          <p>890 Sq. Ft.</p>
          <p>Move-in: 05/14 - 05/24</p>
          <p>Rent starting at $1,293</p>
          <p>The Tuscany 890 SF-FP-FDG</p>
        </article>
        <article>
          <h2>Unit #0113204</h2>
          <p>1 Beds, 1 Baths</p>
          <p>630 Sq. Ft.</p>
          <p>Move-in: 06/01 - 06/10</p>
          <p>Rent starting at $1,108</p>
          <p>The Meridian 630 SF-FP</p>
        </article>
      </section>
    </main>
    """

    out = parse_maa_available_units(html)

    assert out["platform"] == "maa_rendered_units"
    assert len(out["units"]) == 2
    assert out["units"][0]["unit_number"] == "013107"
    assert out["units"][0]["rent"] == 1293
    assert out["units"][0]["floorplan_name"] == "The Tuscany"
    assert {p["floorplan_name"] for p in out["floorplans"]} == {
        "The Tuscany",
        "The Meridian",
    }
