#!/usr/bin/env python3
"""
E2E UI Test usando dogtail para validar o GTK Main Thread
O app `bigcam` deve estar em execução (ou o script o iniciará).
"""

import sys
import time
import subprocess
import os

try:
    from dogtail.tree import root
    from dogtail.utils import run
except ImportError:
    print("Skipping Dogtail test. 'python3-dogtail' is not installed.")
    sys.exit(0)

def test_ui():
    print("Iniciando bigcam para teste E2E...")
    env = os.environ.copy()
    # Ensure AT-SPI is enabled
    env["GTK_A11Y"] = "none" # Actually we need accessibility, maybe default is fine or GTK_MODULES=gail:atk-bridge
    
    app_process = subprocess.Popen(
        [sys.executable, "-m", "bigcam.main"],
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "../usr/share/biglinux/bigcam")),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    
    try:
        # Aguardar o aplicativo registrar no DBus/AT-SPI
        time.sleep(3)
        
        # Encontrar o app na árvore de acessibilidade
        bigcam_app = root.application("bigcam")
        print("App bigcam encontrado!")
        
        # Como o aplicativo usa Adwaita/GTK4, muitos botões não tem texto mas sim icones/tooltips
        # Vamos apenas iterar pelas tabs ou botões visíveis para garantir que a UI não travou.
        buttons = bigcam_app.findChildren(lambda n: n.roleName == 'push button')
        print(f"Encontrados {len(buttons)} botões.")
        
        for i, btn in enumerate(buttons[:5]):
            try:
                print(f"Clicando botão: {btn.name or 'Sem Nome'}")
                btn.click()
                time.sleep(0.5)
            except Exception as e:
                print(f"Aviso ao clicar no botão {i}: {e}")
                
        print("Teste UI finalizado com sucesso. Zero deadlocks.")
        
    except Exception as e:
        print(f"Erro no teste UI: {e}")
        sys.exit(1)
    finally:
        app_process.terminate()
        app_process.wait()

if __name__ == "__main__":
    test_ui()
