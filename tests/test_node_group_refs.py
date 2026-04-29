import unittest
from pathlib import Path

from gn_from_yaml_ir import (
    _BuildContext,
    _find_group_yaml_by_name,
    _get_ir_kind,
    load_ir_from_file,
    load_ir_from_string,
)


class NodeGroupReferenceTests(unittest.TestCase):
    def test_group_yaml_kind_and_outputs_load(self):
        ir = load_ir_from_file("yamls/groups/BoxPart.yaml")
        self.assertEqual(_get_ir_kind(ir), "node_group")
        self.assertEqual(ir["info"]["name"], "BoxPart")
        self.assertEqual(ir["output_socket"][0]["name"], "Geometry")

    def test_find_group_yaml_by_name_from_object_yaml(self):
        context = _BuildContext(ir_cache={}, active_group_files=set())
        source_file = Path("yamls/Table.yaml").resolve()
        found = _find_group_yaml_by_name("BoxPart", source_file, context)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "BoxPart.yaml")

    def test_geometry_group_node_reference_syntax_loads(self):
        ir = load_ir_from_string(
            """
info:
  name: "NodeGroupRefProbe"
node:
  - id: "group_node"
    type: "GeometryNodeGroup"
    props:
      node_tree: "BoxPart"
output:
  Geometry:
    from: "group_node.Geometry"
"""
        )

        group_node = ir["node"][0]
        self.assertEqual(group_node["type"], "GeometryNodeGroup")
        self.assertEqual(group_node["props"]["node_tree"], "BoxPart")

    def test_sample_yamls_load_with_node_group_refs(self):
        sample_dir = Path("yamls")
        for path in sorted(sample_dir.rglob("*.yaml")):
            with self.subTest(path=str(path)):
                ir = load_ir_from_file(path)
                node_ids = [node["id"] for node in ir.get("node", [])]
                self.assertEqual(len(node_ids), len(set(node_ids)))


if __name__ == "__main__":
    unittest.main()
