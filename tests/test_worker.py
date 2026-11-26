import unittest
from worker.worker import classificar, vetor_deterministico


class WorkerTests(unittest.TestCase):
    def test_classifica_texto_positivo(self):
        self.assertEqual(classificar("serviço excelente e rápido")["sentimento"], "positivo")

    def test_classifica_texto_negativo(self):
        self.assertEqual(classificar("serviço ruim e lento")["sentimento"], "negativo")

    def test_vetor_tem_dimensao_e_norma(self):
        vetor = vetor_deterministico("plataforma distribuida")
        self.assertEqual(len(vetor), 64)
        self.assertAlmostEqual(sum(x*x for x in vetor) ** 0.5, 1.0)


if __name__ == "__main__":
    unittest.main()
