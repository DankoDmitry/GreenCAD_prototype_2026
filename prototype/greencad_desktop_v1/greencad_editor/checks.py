"""GUI-independent, read-only checks of an existing plan.

A rule definition is DATA; an operator is CODE. No eval or network access.
Unknown inputs/handlers never yield PASS. Numeric starter thresholds are TEST
project settings, not a normative profile. Catalogue conditions retain sources.
"""
from __future__ import annotations
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import os
import tempfile
from typing import Protocol
from shapely.geometry import Point
from shapely.strtree import STRtree
from shapely.ops import unary_union
from .templates import validate_type_reference, profile_radius, match_plant
from .catalog import DATA, read_json, load_plants
from .models import Drawing, Placement, ProjectType, finite
from .errors import EditorError
from .check_geometry import SiteGeometry, MissingGeometry

LOG = logging.getLogger(__name__)
STATUS_LABELS = {'PASS':'Выполнено', 'FAIL':'Нарушено', 'UNKNOWN':'Недостаточно данных',
                 'ERROR':'Ошибка проверки', 'NOT_APPLICABLE':'Не применяется',
                 'NOT_CHECKED':'Не проверено', 'WARNING':'Рекомендация', 'EMPTY':'Нет посадок'}
RULE_LABELS = {'PROJECT':'Проектное условие', 'PROJECT_TEST':'Тестовый проектный порог',
               'SOURCE_CONDITION':'Условие исходного каталога', 'ADVISORY':'Рекомендация каталога'}
SOIL_KEYS = ('soil_drained','soil_loose','soil_saline','soil_compacted','stagnant_water')
ENGINE_VERSION = '0.6.0'


def load_rules(reference=None):
    from .reference_data import default_bundle
    return (reference or default_bundle()).rules()


def catalog_digest(reference=None):
    from .reference_data import default_bundle
    bundle = reference or default_bundle()
    return hashlib.sha256((ENGINE_VERSION+':'+bundle.id).encode()).hexdigest()


def default_settings(reference=None):
    return {'schema_version':1,'enabled':[r['id'] for r in load_rules(reference) if r['default_enabled']],
            'thresholds_m':{},'site':{'A1':None, 'territory':None, 'ordinary_territory':None,
                                    **{k:None for k in SOIL_KEYS}},
            'type_ranges':{},'confirmed_absent':[]}


def validate_settings(value, reference=None):
    if not isinstance(value,dict) or value.get('schema_version') != 1:
        raise EditorError('Неизвестная версия настроек проверок.')
    if set(value)-set(default_settings(reference)):
        raise EditorError('Неизвестные поля настроек проверок.')
    result=deepcopy(value)
    ids=result.get('enabled',[])
    if not isinstance(ids,list) or len(ids)>200 or any(not isinstance(k,str) or not k for k in ids) or len(ids)!=len(set(ids)):
        raise EditorError('Нужен список уникальных ID проверок (не более 200).')
    result['enabled']=ids
    thresholds=result.setdefault('thresholds_m',{})
    if not isinstance(thresholds,dict):raise EditorError('Отступы должны быть объектом JSON.')
    for k,v in list(thresholds.items()):
        v=finite(v,f'Отступ {k}')
        if not 0<=v<=1000000:raise EditorError('Отступ должен быть от 0 до 1 000 000 м.')
        thresholds[k]=v
    site=result.setdefault('site',{})
    if not isinstance(site,dict) or set(site)-set(default_settings(reference)['site']):
        raise EditorError('Неизвестные параметры территории.')
    for k in (*SOIL_KEYS,'ordinary_territory'):
        if site.get(k) is not None and not isinstance(site[k],bool):
            raise EditorError(f'{k}: требуется true/false/null.')
    if site.get('A1') is not None:
        site['A1']=finite(site['A1'],'A1')
        if not 0<=site['A1']<=1:raise EditorError('A1: сценарная шкала от 0 до 1.')
    from .reference_data import default_bundle
    facts=(reference or default_bundle()).facts()
    if site.get('territory') is not None and site['territory'] not in {t['id'] for t in facts['territories']}:
        raise EditorError('Неизвестная категория территории.')
    ranges=result.setdefault('type_ranges',{})
    if not isinstance(ranges,dict):raise EditorError('Диапазоны типов должны быть объектом JSON.')
    for tid,pair in list(ranges.items()):
        if not isinstance(pair,list) or len(pair)!=2:raise EditorError(f'{tid}: нужны две границы диапазона.')
        lo,hi=finite(pair[0],'Нижняя граница'),finite(pair[1],'Верхняя граница')
        if not 0<=lo<=hi<=1:raise EditorError('Диапазон A1: 0 ≤ минимум ≤ максимум ≤ 1.')
        ranges[tid]=[lo,hi]
    absent=result.setdefault('confirmed_absent',[])
    if not isinstance(absent,list) or any(not isinstance(k,str) for k in absent) or len(absent)!=len(set(absent)):
        raise EditorError('Неверный список подтверждённо отсутствующих объектов.')
    return result


@dataclass
class CheckResult:
    rule_id:str
    label:str
    kind:str
    status:str
    reason:str
    actual:object=None
    required:object=None
    unit:str=''
    object_ids:list[str]=field(default_factory=list)
    evidence:dict=field(default_factory=dict)
    sources:list[dict]=field(default_factory=list)


@dataclass
class ValidationRequest:
    drawing:Drawing
    placements:list[Placement]
    types:dict[str,ProjectType]
    settings:dict
    fingerprint:str
    catalog_hash:str
    reference:object=None


def fingerprint_state(drawing, state, reference):
    """One fingerprint for live sessions and proposed immutable states."""
    payload={'source':drawing.source_sha256,'metres_per_unit':drawing.metres_per_unit,
             'state':state,'catalog':catalog_digest(reference)}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def fingerprint(session):
    return fingerprint_state(session.drawing, session.state(), session.reference)


def make_request(session):
    # No Tk objects, display primitives, undo history, or shared mutable parameters.
    drawing=replace(session.drawing,
                    features=[replace(f,properties=deepcopy(f.properties),primitives=[]) for f in session.drawing.features],
                    diagnostics=deepcopy(session.drawing.diagnostics),
                    layer_mapping=deepcopy(session.drawing.layer_mapping))
    return ValidationRequest(drawing,deepcopy(list(session.placements.values())),deepcopy(session.types),
                             validate_settings(session.check_settings, session.reference),fingerprint(session),
                             catalog_digest(session.reference),session.reference)


def aggregate(results, *, mandatory_only=False):
    # A component map may display advice separately. Do not let absent advisory
    # data turn a completed mandatory intersection into permission or a ban.
    if mandatory_only:
        results = [r for r in results if r.kind != 'ADVISORY']
    if not results:return 'NOT_CHECKED'
    statuses={r.status for r in results}
    for status in ('FAIL','ERROR','UNKNOWN'):
        if status in statuses:return status
    hard=[r for r in results if r.kind!='ADVISORY' and r.status!='NOT_APPLICABLE']
    if not hard:return 'NOT_CHECKED'
    return 'WARNING' if 'WARNING' in statuses else 'PASS'


class RuleOperator(Protocol):
    def __call__(self, context:'CheckContext', rule:dict, placement:Placement) -> CheckResult: ...


class CheckContext:
    def __init__(self,request, *, template_only=False):
        self.request=request
        self.template_only=template_only
        self._unions={}
        self._matches={}
        self._spacing_limits={}
        self.settings=request.settings
        self.site=SiteGeometry(request.drawing)
        from .reference_data import default_bundle
        self.reference=request.reference or default_bundle()
        self.facts=self.reference.facts()
        for t in request.types.values(): validate_type_reference(t, self.reference)
        self.plants={p['id']:p for p in self.reference.plants()}
        radii=[profile_radius(t) or 0.0 for t in request.types.values()]
        self.max_radius=max(radii,default=0.0)
        self.points=[Point(p.x,p.y) for p in request.placements]
        self.index=STRtree(self.points) if self.points else None
        self.indices={p.id:i for i,p in enumerate(request.placements)}

    def outcome(self,rule,status,reason,**kwargs):
        evidence=kwargs.pop('evidence',{})
        if rule.get('field_id'):evidence['field_id']=rule['field_id']
        evidence['reference_bundle_sha256']=self.reference.id
        evidence['binding']=deepcopy(rule.get('binding', {}))
        if rule.get('field_id'):
            origin=rule.get('binding',{}).get('field_registry','plants')
            evidence['field_location']=self.reference.location('fields',rule['field_id'],origin)
        return CheckResult(rule['id'],rule['label'],rule['kind'],status,reason,
                           evidence=evidence,sources=deepcopy(rule.get('provenance',[])),**kwargs)

    def effective_plant_id(self,p):
        if self.template_only: return None
        return p.plant_id or self.request.types[p.type_id].plant_id

    def profile(self,p):
        return self.request.types[p.type_id].template

    def plant(self,p):
        pid=self.effective_plant_id(p)
        if not pid or pid not in self.plants:
            raise MissingGeometry('Не выбрана известная запись растения (индивидуально или в проектном типе).')
        return self.plants[pid], self.reference

    def field(self,rule,area=False):
        return self.site.field(rule['object_types'],rule['roles'],
                               confirmed_absent=self.settings['confirmed_absent'],require_area=area,
                               feature_ids=rule.get('feature_ids'))

    def threshold(self,rule,p=None):
        value=self.settings['thresholds_m'].get(rule['id'],rule.get('threshold_m'))
        profile=self.profile(p) if p is not None else None
        extra=profile['minimum_distances_m'].get(rule['id']) if profile else None
        values=[finite(v,'Порог') for v in (value,extra) if v is not None]
        if not values:raise MissingGeometry('Не задан порог проверки.')
        return max(values)

    def radius(self,p):
        # Explicit design envelope, never symbol radius.
        return profile_radius(self.request.types[p.type_id]) or 0.0

    def union(self,rule,field):
        key=rule['id']
        if key not in self._unions:self._unions[key]=unary_union([v.geometry for v in field.items])
        return self._unions[key]

    def match(self,p):
        pid=self.effective_plant_id(p); key=(p.type_id,pid)
        if key not in self._matches:
            self._matches[key]=match_plant(self.request.types[p.type_id],pid,self.reference)
        return deepcopy(self._matches[key])


def inside(c,r,p):
    f=c.field(r,area=True);f.require()
    if f.absent:return c.outcome(r,'FAIL','Подтверждено отсутствие зелёных зон.')
    point=Point(p.x,p.y);hits=f.covering(point);radius=c.radius(p)
    actual=float(c.union(r,f).boundary.distance(point)) if radius else None
    passed=bool(hits) and (radius==0 or actual>=radius)
    return c.outcome(r,'PASS' if passed else 'FAIL',
        ('Центр внутри зелёной зоны (граница включена).' if hits else 'Центр за пределами всех заданных зелёных зон.')
        + (f' Проверен резерв кроны {radius:g} м по расстоянию до границы, включая отверстия.' if radius else ''),
        object_ids=[v.feature.id for v in hits],actual=actual if radius else bool(hits),
        required=radius if radius else True,unit='m' if radius else '',
        evidence={'calculation_origin':'PROJECT_TYPE' if radius else 'CENTER_ONLY','type_id':p.type_id})


def outside(c,r,p):
    f=c.field(r,area=True);point=Point(p.x,p.y);hits=f.covering(point);radius=c.radius(p)
    near=f.nearest(point) if radius else None
    too_close=bool(near and near[0]<radius)
    passed=not hits and not too_close
    return c.outcome(r,'PASS' if passed else 'FAIL',
        ('Центр вне заданных запрещённых площадей.' if not hits else 'Центр попал в запрещённую площадь.')
        + (f' Проверен резерв кроны {radius:g} м; касание границы допускается при центре снаружи.' if radius else ''),
        object_ids=[v.feature.id for v in hits] or ([near[1].id] if too_close else []),
        actual=near[0] if radius and near else bool(hits),required=radius if radius else False,
        unit='m' if radius else '',evidence={'absence_confirmed':f.absent,'type_id':p.type_id})


def distance(c,r,p):
    f=c.field(r)
    nearest=f.nearest(Point(p.x,p.y))
    limit=c.threshold(r,p)
    if nearest is None:
        return c.outcome(r,'PASS','Отсутствие объектов данной категории подтверждено пользователем.',required=limit,unit='m')
    value,asset=nearest
    return c.outcome(r,'PASS' if value>=limit else 'FAIL',
                     f'Расстояние {value:.9g} м; проектный минимум {limit:.9g} м. Корнезащита не уменьшает этот порог.',
                     actual=value,required=limit,unit='m',object_ids=[asset.id],
                     evidence={'geometry_role':asset.geometry_role,'threshold_origin':'PROJECT_TEST',
                               'type_minimum_m':(c.profile(p) or {}).get('minimum_distances_m',{}).get(r['id']),
                               'type_id':p.type_id, 'root_protection':p.root_protection})


def spacing(c,r,p):
    limit=c.threshold(r,p);point=Point(p.x,p.y)
    if r['id'] not in c._spacing_limits:
        c._spacing_limits[r['id']]=max((c.threshold(r,other) for other in c.request.placements),default=limit)
    search=max(limit, c.radius(p)+c.max_radius, c._spacing_limits[r['id']])
    indices=c.index.query(point,predicate='dwithin',distance=search) if c.index is not None else []
    conflicts=[]
    for idx in sorted(indices):
        other=c.request.placements[int(idx)]
        if other.id==p.id:continue
        required=max(limit,c.threshold(r,other),c.radius(p)+c.radius(other))
        d=float(point.distance(c.points[int(idx)]))
        if d<required:conflicts.append({'distance':d,'required':required,'other_id':other.id})
    return c.outcome(r,'FAIL' if conflicts else 'PASS',
        'Нарушены межпосадочные условия.' if conflicts else 'Межпосадочные условия выполнены: общий минимум, минимумы обоих типов и сумма заданных резервов кроны.',
        actual=min((v['distance'] for v in conflicts),default=None),required=limit,unit='m',
        object_ids=[v['other_id'] for v in conflicts],
        evidence={'threshold_origin':'PROJECT_TEST','pair_conflicts':conflicts,'type_id':p.type_id})


def territory(c,r,p):
    plant,fact=c.plant(p)
    site=c.settings['site']
    if site.get('ordinary_territory') is not True:
        return c.outcome(r,'UNKNOWN','Не подтверждена обычная территория без специального режима. Сноски о мемориалах, ООЗТ и других особых территориях здесь не исполняются.')
    keys=[t['id'] for t in c.reference.territories()]
    key=site.get('territory')
    if key not in keys:return c.outcome(r,'UNKNOWN','Не выбрана категория территории.')
    fact=c.reference.value(plant['id'],'plant.territory_mark',{'territory_id':key})
    mark={'PLUS':'+','MINUS':'-'}.get(fact['value'],'?') if fact['status']=='KNOWN' else '?'
    status={'+':'PASS','-':'WARNING'}.get(mark,'UNKNOWN')
    result=c.outcome(r,status,f'Исходная отметка: {mark}. Минус — рекомендация каталога, не нормативный запрет.',
                     actual=mark,required='+',evidence={'plant_id':plant['id'],'territory':key,'value_record':fact})
    result.sources=[{'source_id':fact.get('source_id',plant['source']), 'locator':fact.get('locator','')+'; '+key}]
    return result


def source_boolean(c,r,p):
    parameter=r.get('plant_parameter')
    if not parameter:return c.outcome(r,'UNKNOWN','Не скомпилирована связь с характеристикой растения.')
    profile=c.profile(p);pid=c.effective_plant_id(p);components=[]
    def one(requirement,origin):
        ev={'plant_id':pid,'plant_parameter':parameter,'value_record':requirement,
            'site_parameter':r['site_parameter'],'site_value_origin':'USER_SCENARIO',
            'calculation_origin':origin,'type_id':p.type_id}
        if requirement['status']!='KNOWN' or type(requirement['value']) is not bool:
            return c.outcome(r,'UNKNOWN','Требование не задано. Неизвестное не означает устойчивость.',evidence=ev)
        if requirement['value'] is False:
            return c.outcome(r,'NOT_APPLICABLE','Явно указано отсутствие этого требования.',evidence=ev)
        value=c.settings['site'].get(r['site_parameter'])
        if value is None:return c.outcome(r,'UNKNOWN','Не задан параметр участка: '+r['site_parameter'],evidence=ev)
        result=c.outcome(r,'PASS' if value==r['expected'] else 'FAIL',
            'Требование '+('растения из пакета' if origin=='CATALOGUE' else 'проектного типа')+' сопоставлено с условиями участка.',
            actual=value,required=r['expected'],evidence=ev)
        result.sources=[{'source_id':requirement.get('source_id','PROJECT_TYPE'),'locator':requirement.get('locator',p.type_id)}]
        return result
    if pid:
        plant,_=c.plant(p)
        components.append(one(c.reference.value(plant['id'],parameter),'CATALOGUE'))
    if profile is not None:
        v=profile['requirements'].get(parameter)
        components.append(one({'parameter_id':parameter,'value':v,'status':'KNOWN' if v is not None else 'UNKNOWN',
                               'source_id':'PROJECT_TYPE','locator':p.type_id+' / requirements / '+parameter},'PROJECT_TYPE'))
        # A bound plant can supply a missing template requirement, not vice versa.
        if pid and v is None:components.pop()
    if not components:
        c.plant(p)  # consistent legacy missing-plant message
    if len(components)==1:return components[0]
    statuses={v.status for v in components}
    status=next((x for x in ('FAIL','ERROR','UNKNOWN','PASS') if x in statuses),'NOT_APPLICABLE')
    evidence_components=[{'origin':x.evidence.get('calculation_origin'),'status':x.status,
                          'required':x.required,'actual':x.actual} for x in components]
    result=components[0]
    result.status=status;result.reason='Растение и профиль типа проверены независимо; более мягкое значение не отменяет другое требование.'
    result.evidence['components']=evidence_components
    result.sources += [v for x in components[1:] for v in x.sources]
    return result


def interval(c,r,p):
    value=c.settings['site'].get('A1');profile=c.profile(p)
    legacy=c.settings['type_ranges'].get(p.type_id)
    own=profile['moisture_range'] if profile else None
    pairs=[v for v in (legacy,own) if v is not None]
    bounds=[max(v[0] for v in pairs),min(v[1] for v in pairs)] if pairs else None
    if value is None or bounds is None:
        return c.outcome(r,'UNKNOWN','Нужны A1 участка и диапазон A1 проектного типа. Это сценарные параметры, не свойства растения из ГОСТа.',actual=value,required=bounds)
    return c.outcome(r,'PASS' if bounds[0]<=value<=bounds[1] else 'FAIL',
        'Сценарная влажность A1 сопоставлена с диапазоном типа. При двух заданных диапазонах взято их пересечение.',
        actual=value,required=bounds,unit='score_0_1',evidence={'origin':'USER_SCENARIO','type_id':p.type_id,
        'profile_range':own,'settings_range':legacy})


def template_root(c,r,p):
    mode=c.profile(p)['root_protection']
    passed=mode=='ANY' or p.root_protection==(mode=='REQUIRED')
    return c.outcome(r,'PASS' if passed else 'FAIL','Проверен проектный режим корнезащиты. Это не разрешение технологии для вида и не снижение отступов.',
                    actual=p.root_protection,required=mode,evidence={'type_id':p.type_id,'origin':'PROJECT_TYPE'})


def template_match(c,r,p):
    result=c.match(p)
    return c.outcome(r,result['status'],'Растение проверено по критериям типа. Пространственные правила выполняются отдельно; характеристики шаблона не приписываются растению.',
                    evidence={'type_id':p.type_id,'plant_id':c.effective_plant_id(p),'matching':result})


TEMPLATE_RULES=[
    {'id':'template.root_mode','label':'Режим корнезащиты проектного типа','kind':'PROJECT','operator':'template_root',
     'evaluation_scope':'SITE_POINT','provenance':[{'source_id':'PROJECT_TYPE','locator':'root_protection'}]},
    {'id':'template.catalog_match','label':'Растение соответствует проектному типу','kind':'PROJECT','operator':'template_match',
     'evaluation_scope':'SITE_POINT','provenance':[{'source_id':'PROJECT_TYPE','locator':'Критерии шаблона'}]}]


OPERATORS:dict[str,RuleOperator]={'inside':inside,'outside':outside,'distance':distance,'spacing':spacing,
                               'territory':territory,'source_boolean':source_boolean,'range':interval,
 'template_root':template_root,'template_match':template_match}


class PointEvaluator:
    """Shared execution path for placed plants and hypothetical map probes.

    Prepare geometry/catalog once; evaluate without changing request. Results
    are not cached by point: exact clicks and plan checks use original XY.
    """
    def __init__(self, request: ValidationRequest, *, rules=None, operators=None, template_only=False):
        self.request = request
        self.rules = load_rules(request.reference) if rules is None else deepcopy(rules)
        self.rules.extend(deepcopy(TEMPLATE_RULES))
        self.by_id = {r['id']: r for r in self.rules}
        self.handlers = dict(OPERATORS)
        if operators is not None:
            self.handlers.update(operators)
        self.context = CheckContext(request, template_only=template_only)

    def append_placement(self, placement: Placement) -> None:
        """Extend a PRIVATE working request for incremental generation.

        The caller must own its request (never pass the live editor state).
        The same spacing operator is reused; the GEOS index is rebuilt on
        acceptance only. Static geometry/catalogue caches are retained.
        """
        placement = deepcopy(placement)
        placement.validate()
        if placement.type_id not in self.request.types:
            raise EditorError('Неизвестный тип добавляемой расчётной посадки.')
        if placement.id in self.context.indices:
            raise EditorError('Повтор ID в расчётном плане.')
        self.context.indices[placement.id] = len(self.request.placements)
        self.request.placements.append(placement)
        self.context.points.append(Point(placement.x, placement.y))
        self.context.index = STRtree(self.context.points)
        self.context._spacing_limits.clear()

    def effective_ids(self, placement, enabled=None):
        ids=list(self.request.settings['enabled'] if enabled is None else enabled)
        profile=self.context.profile(placement)
        # No selected checks still means NOT_CHECKED. Explicit automatic criteria
        # supplement, never replace, the chosen rules.
        if ids and profile is not None:
            if profile['root_protection']!='ANY' and 'template.root_mode' not in ids:ids.append('template.root_mode')
            if self.context.effective_plant_id(placement) and 'template.catalog_match' not in ids:ids.append('template.catalog_match')
        return ids

    def evaluate(self, placement: Placement, enabled=None) -> list[CheckResult]:
        results = []
        for rid in self.effective_ids(placement,enabled):
            rule = self.by_id.get(rid)
            if rule is None:
                results.append(CheckResult(rid, rid, 'PROJECT', 'UNKNOWN',
                                           'ID проверки отсутствует в загруженном списке.'))
                continue
            try:
                op = self.handlers.get(rule['operator'])
                if op is None:
                    result = self.context.outcome(rule, 'UNKNOWN',
                        'Обработчик ' + rule['operator'] + ' не реализован. Правило не пропущено.')
                else:
                    result = op(self.context, rule, placement)
                if not isinstance(result, CheckResult) or result.status not in STATUS_LABELS:
                    raise ValueError('Некорректный ответ обработчика правила.')
            except MissingGeometry as exc:
                result = self.context.outcome(rule, 'UNKNOWN', str(exc))
            except Exception as exc:
                LOG.exception('Ошибка проверки %s для %s', rid, placement.id)
                result = self.context.outcome(rule, 'ERROR', f'{type(exc).__name__}: {exc}')
            results.append(result)
        return results

    def row(self, placement: Placement, enabled=None):
        p = placement
        results = self.evaluate(p, enabled)
        return {'id': p.id, 'label': p.label, 'type_id': p.type_id,
                'plant_id': p.plant_id or self.request.types[p.type_id].plant_id,
                'x': p.x, 'y': p.y, 'root_protection': p.root_protection,
                'calculation_target':'PLANT_AND_TEMPLATE' if self.context.profile(p) and self.context.effective_plant_id(p) else ('TEMPLATE' if self.context.profile(p) else 'PLANT_OR_PROJECT'),
                'status': aggregate(results), 'checks': [asdict(r) for r in results]}


class ValidationCancelled(EditorError):
    pass


def validate_plan(request:ValidationRequest, *, rules=None, operators=None, cancel=None, progress=None):
    evaluator = PointEvaluator(request, rules=rules, operators=operators)
    context, by_id = evaluator.context, evaluator.by_id
    enabled = request.settings['enabled']
    rows = []
    for index, placement in enumerate(request.placements):
        if cancel is not None and cancel.is_set():
            raise ValidationCancelled('Проверка отменена.')
        rows.append(evaluator.row(placement))
        if progress is not None: progress(index + 1, len(request.placements))
    counts=dict(Counter(row['status'] for row in rows))
    flat=[CheckResult(**v) for row in rows for v in row['checks']]
    status=aggregate(flat) if rows else 'EMPTY'
    executed_ids=list(dict.fromkeys(r.rule_id for r in flat))
    return {'schema':'greencad.validation-report','schema_version':1,'engine_version':ENGINE_VERSION,
            'created_at':datetime.now(timezone.utc).isoformat(),'status':status,'counts':counts,
            'source':{'name':request.drawing.source_name,'sha256':request.drawing.source_sha256,
                      'metres_per_unit':request.drawing.metres_per_unit},
            'fingerprint':request.fingerprint,'catalog_sha256':request.catalog_hash,
            'enabled_rules':enabled,'settings':deepcopy(request.settings),
            'rules':[by_id[rid] for rid in executed_ids if rid in by_id],
            'automatic_rules':[rid for rid in executed_ids if rid not in enabled],
            'project_types':{tid:asdict(t) for tid,t in request.types.items()},
            'sources':context.facts['sources'],'reference_bundle':context.reference.summary(),
            'scope':'Только выбранные проверки при объявленных исходных данных. Не комплексное нормативное заключение. Тестовые проектные пороги не являются нормами.',
            'placements':rows,'geometry_issues':context.site.global_errors}


def report_envelope(session):
    report=getattr(session,'validation_report',None)
    if report is None:return {'state':'NOT_CHECKED','report':None}
    current=report.get('fingerprint')==fingerprint(session)
    return {'state':'CURRENT' if current else 'STALE','report':deepcopy(report)}


def save_report(payload,path):
    """Atomic JSON sidecar. Never permits overriding a DXF/GCP/Python source."""
    path=Path(path)
    if path.suffix.lower()!='.json':raise EditorError('Отчёт сохраняется только в JSON.')
    data=json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8')
    fd,tmp=tempfile.mkstemp(prefix='.checks_',suffix='.json',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(data)
        os.replace(tmp,path)
    finally:Path(tmp).unlink(missing_ok=True)
