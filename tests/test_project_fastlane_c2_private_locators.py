import subprocess,sys
def test_projection_refuses_prod_root(tmp_path):
 r=subprocess.run([sys.executable,'scripts/project_fastlane_c2_private_locators.py','--root','/opt/no','--out',str(tmp_path/'x')],capture_output=True,text=True);assert r.returncode and 'absolute-prod-path' in r.stderr
