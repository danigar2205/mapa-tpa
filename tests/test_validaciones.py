import math
import unittest

from app import validar_coordenadas


class ValidarCoordenadasTests(unittest.TestCase):
    def test_acepta_coordenadas_cero(self):
        self.assertEqual(validar_coordenadas(0, 0), (0.0, 0.0))

    def test_acepta_limites_geograficos(self):
        self.assertEqual(validar_coordenadas(90, 180), (90.0, 180.0))
        self.assertEqual(validar_coordenadas(-90, -180), (-90.0, -180.0))

    def test_rechaza_valores_ausentes(self):
        self.assertIsNone(validar_coordenadas(None, 0))
        self.assertIsNone(validar_coordenadas(0, None))

    def test_rechaza_coordenadas_fuera_de_rango(self):
        self.assertIsNone(validar_coordenadas(91, 0))
        self.assertIsNone(validar_coordenadas(0, 181))
        self.assertIsNone(validar_coordenadas(-91, 0))
        self.assertIsNone(validar_coordenadas(0, -181))

    def test_rechaza_nan_e_infinito(self):
        self.assertIsNone(validar_coordenadas(math.nan, 0))
        self.assertIsNone(validar_coordenadas(0, math.inf))

    def test_rechaza_texto_no_numerico(self):
        self.assertIsNone(validar_coordenadas("norte", "oeste"))


if __name__ == "__main__":
    unittest.main()
