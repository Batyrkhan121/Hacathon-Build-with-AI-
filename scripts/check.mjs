import fs from 'node:fs';
import vm from 'node:vm';
const root = new URL('../dist/', import.meta.url);
for (const file of ['index.html','styles.css','app.js','projects.js','assets/CV_Batyrkhan_FullStack.pdf']) if(!fs.existsSync(new URL(file,root))) throw new Error(`Missing ${file}`);
const source=fs.readFileSync(new URL('projects.js',root),'utf8');
const projects=vm.runInNewContext(source+';PROJECTS');
if(projects.length!==14)throw new Error('Expected 14 projects');
for(const p of projects){if(!fs.existsSync(new URL('assets/'+p.image,root)))throw new Error(`Missing image ${p.image}`);if(p.demo){const base=p.demo.split('?')[0];if(!fs.existsSync(new URL(base+'index.html',root)))throw new Error(`Missing demo ${base}`);}}
new vm.Script(fs.readFileSync(new URL('app.js',root),'utf8'));
console.log(`Portfolio checked: ${projects.length} projects, assets and demos present.`);
