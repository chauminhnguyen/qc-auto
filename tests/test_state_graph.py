import unittest

from capture_app.state.state_graph import StateGraph


class TopologicalSortTests(unittest.TestCase):
    def test_prerequisites_precede_dependent_states(self):
        graph = StateGraph()
        graph.add_edge("login", "cart")
        graph.add_edge("cart", "checkout")

        self.assertEqual(graph.topological_sort(), ["login", "cart", "checkout"])

    def test_shared_prerequisite_precedes_each_dependent(self):
        graph = StateGraph()
        graph.add_edge("login", "catalog")
        graph.add_edge("login", "cart")

        order = graph.topological_sort()

        self.assertLess(order.index("login"), order.index("catalog"))
        self.assertLess(order.index("login"), order.index("cart"))


if __name__ == "__main__":
    unittest.main()