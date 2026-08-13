import unittest


class NmapRouteRegistrationTests(unittest.TestCase):
    def test_external_route_table_omits_the_xml_parser_endpoint(self) -> None:
        from app.api import router as router_module
        from fastapi import APIRouter

        target = APIRouter()
        router_module.include_internal_nmap_routes(target, enabled=False)

        self.assertNotIn("/nmap/xml-import", {route.path for route in target.routes})

    def test_internal_route_table_mounts_the_xml_parser_endpoint(self) -> None:
        from app.api import router as router_module
        from fastapi import APIRouter

        target = APIRouter()
        router_module.include_internal_nmap_routes(target, enabled=True)

        self.assertIn("/nmap/xml-import", {route.path for route in target.routes})


if __name__ == "__main__":
    unittest.main()
