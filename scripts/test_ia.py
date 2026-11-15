import importlib.machinery,importlib.util,json,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
S=Path(__file__).resolve().parent;sys.path.insert(0,str(S))
loader=importlib.machinery.SourceFileLoader('thumb',str(S/'yt-thumbnail'))
spec=importlib.util.spec_from_loader(loader.name,loader);m=importlib.util.module_from_spec(spec);loader.exec_module(m)
import ia_motor as ia
class Teste(unittest.TestCase):
 def test_imagem_codex_sem_reserva(self):
  img=Image.new('RGB',(1280,720))
  with patch.object(m,'_generate_image_codex',return_value=img),patch.object(m.subprocess if hasattr(m,'subprocess') else subprocess,'run') as run:
   self.assertIs(m.generate_ai_image('x'),img);run.assert_not_called()
 def test_falha_codex_tenta_gpu_local(self):
  def run(cmd,**kwargs):
   self.assertEqual(cmd[1],'gpu');self.assertTrue(cmd[-2].endswith('imagem_reserva.py'))
   Image.new('RGB',(1280,720)).save(cmd[-1]);return subprocess.CompletedProcess(cmd,0,'','')
  with patch.object(m,'_generate_image_codex',side_effect=RuntimeError('indisponível')),patch.object(subprocess,'run',side_effect=run):
   self.assertEqual(m.generate_ai_image('x').size,(1280,720))
 def test_api_imagem_recusada(self):
  import copy
  d=copy.deepcopy(ia.carregar());d['etapas']['capa'].update(tipo='api',motor='openai')
  with self.assertRaises(ValueError):ia.validar(d)
 def test_alias_legacy_nao_muda_modelo_do_arquivo(self):
  source=(S/'yt-clip').read_text();self.assertIn('analise_cortes.py',source);self.assertNotIn('api.anthropic.com',source);self.assertNotIn('openrouter.ai/api',source)
if __name__=='__main__':unittest.main()
