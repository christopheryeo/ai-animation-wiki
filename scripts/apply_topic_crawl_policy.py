#!/usr/bin/env python3
"""Apply approved conservative topic-crawl resolution policy to held inputs."""
from __future__ import annotations
import argparse, json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
from enrich_radar_inputs import split_note, parse_frontmatter, replace_fields

def main():
    p=argparse.ArgumentParser(); p.add_argument('--assessment', type=Path, action='append', required=True); p.add_argument('--output', type=Path, required=True); a=p.parse_args()
    rows={}
    for artifact in a.assessment:
        for x in json.loads(artifact.read_text()).get('assessments',[]):
            if x.get('consensus',{}).get('reviewRequired'): rows[x['path']]=x
    resolved=[]
    for raw_path,x in sorted(rows.items()):
        path=Path(raw_path)
        if not path.exists(): continue
        lines,body=split_note(path.read_text()); c=x['consensus']; auto=c['autoApplicable']; updates={}
        if not auto.get('tone'): updates['tone']='Factual'
        if not auto.get('toneSentiment'): updates['toneSentiment']='Neutral'
        if not auto.get('eventType'): updates['eventType']='Unfacilitated'
        if not auto.get('metadata'): updates['category']='Non-institutional'
        if not auto.get('tags'): updates['tags']=['#source']
        if updates: path.write_text('---\n'+'\n'.join(replace_fields(lines,updates))+'\n---\n\n'+body,encoding='utf-8')
        resolved.append({'path':raw_path,'articleId':x['articleId'],'disposition':'policy-resolved','updates':updates})
    a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps({'policyVersion':1,'resolved':resolved},indent=2)+'\n')
    print(json.dumps({'resolved':len(resolved)}))
if __name__=='__main__': main()
