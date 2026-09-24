from __future__ import annotations

from etl.comp_scraping.engines.static_floorplans import parse_static_floorplans


def test_parse_static_floorplans_extracts_bed_bath_sqft_and_call_rent() -> None:
    html = """
    <html><body>
      <h2>Type F One Bedroom / One Bath - 602 Sq. Ft.*</h2>
      <p>Bedroom(s) - 1</p>
      <p>Bathroom(s) - 1</p>
      <p>Sq. Ft.* - 602</p>
      <p>Rent - Please Call</p>
      <h2>Plan B Two Bedroom / One Bath - 899 Sq. Ft.*</h2>
      <p>Bedroom(s) - 2</p>
      <p>Bathroom(s) - 1</p>
      <p>Sq. Ft.* - 899</p>
      <p>Rent - $1,200</p>
    </body></html>
    """

    parsed = parse_static_floorplans(html)

    assert parsed["platform"] == "static_floorplans"
    assert parsed["floorplans"] == [
        {
            "floorplan_name": "Type F",
            "beds": 1,
            "baths": 1.0,
            "sqft": 602.0,
            "rent_min": None,
            "rent_max": None,
        },
        {
            "floorplan_name": "Plan B",
            "beds": 2,
            "baths": 1.0,
            "sqft": 899.0,
            "rent_min": 1200.0,
            "rent_max": 1200.0,
        },
    ]
