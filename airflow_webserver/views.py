# -*- coding: utf-8 -*-
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from past.builtins import basestring, unicode

import ast
import logging
import os
import pkg_resources
import socket
from functools import wraps
from datetime import datetime, timedelta
import dateutil.parser
import copy
import math
import json
import bleach
from collections import defaultdict

import inspect
from textwrap import dedent
import traceback

import sqlalchemy as sqla
from sqlalchemy import or_, desc, and_, union_all

from flask import (
    g, redirect, url_for, request, Markup, Response, render_template,
    make_response, abort, flash)
from flask._compat import PY2

from flask_appbuilder import BaseView, ModelView, IndexView, expose, has_access, AppBuilder
from flask_appbuilder.models.sqla.interface import SQLAInterface
from flask_appbuilder.actions import action
from flask_appbuilder.widgets import RenderTemplateWidget, FormWidget

from flask_babel import lazy_gettext

from jinja2.sandbox import ImmutableSandboxedEnvironment
from jinja2 import escape

import markdown
import nvd3

from wtforms import Form, SelectField, TextAreaField, validators

from pygments import highlight, lexers
from pygments.formatters import HtmlFormatter

import airflow
from airflow import configuration as conf
from airflow import models, jobs
from airflow import settings
from airflow.api.common.experimental.mark_tasks import set_dag_run_state
from airflow.exceptions import AirflowException
from airflow.settings import Session
from airflow.models import XCom, DagRun
from airflow.ti_deps.dep_context import DepContext, QUEUE_DEPS, SCHEDULER_DEPS

from airflow.models import BaseOperator
from airflow.operators.subdag_operator import SubDagOperator

from airflow.utils.json import json_ser
from airflow.utils.state import State
from airflow.utils.db import provide_session
from airflow.utils.helpers import alchemy_to_dict
from airflow.utils.dates import infer_time_unit, scale_time_units

from airflow_webserver import appbuilder, db
from airflow_webserver.forms import DateTimeForm, DateTimeWithNumRunsForm, DagRunForm, ConnectionForm
from airflow_webserver.security import is_view_only
from airflow_webserver.validators import GreaterEqualThan
from airflow_webserver import utils as wwwutils

QUERY_LIMIT = 100000
CHART_LIMIT = 200000

PAGE_SIZE = conf.getint('webserver', 'page_size')

dagbag = models.DagBag(settings.DAGS_FOLDER)

FILTER_BY_OWNER = False

if conf.getboolean('webserver', 'FILTER_BY_OWNER'):
    # filter_by_owner if authentication is enabled and filter_by_owner is true
    FILTER_BY_OWNER = not appbuilder.app.config['LOGIN_DISABLED']

def task_instance_link(attr):
    dag_id = bleach.clean(attr.get('dag_id'))
    task_id = bleach.clean(attr.get('task_id'))
    execution_date = attr.get('execution_date')
    url = url_for(
        'Airflow.task',
        dag_id=dag_id,
        task_id=task_id,
        execution_date=execution_date.isoformat())
    url_root = url_for(
        'Airflow.graph',
        dag_id=dag_id,
        root=task_id,
        execution_date=execution_date.isoformat())
    return Markup(
        """
        <span style="white-space: nowrap;">
        <a href="{url}">{task_id}</a>
        <a href="{url_root}" title="Filter on this task and upstream">
        <span class="glyphicon glyphicon-filter" style="margin-left: 0px;"
            aria-hidden="true"></span>
        </a>
        </span>
        """.format(**locals()))

def state_token(state):
    color = State.color(state)
    return Markup(
        '<span class="label" style="background-color:{color};">'
        '{state}</span>'.format(**locals()))

def state_f(attr):
    state = attr.get('state')
    return state_token(state)

def nobr_f(field):
    def nobr_f_helper(attr):
        f = attr.get(field)
        return Markup("<nobr>{}</nobr>".format(f))
    return nobr_f_helper

def datetime_f(field):
    def datetime_f_helper(attr):
        f = attr.get(field)
        f = f.isoformat() if f else ''
        if datetime.now().isoformat()[:4] == f[:4]:
            f = f[5:]
        return Markup("<nobr>{}</nobr>".format(f))
    return datetime_f_helper

def dag_link(attr):
    dag_id = bleach.clean(attr.get('dag_id'))
    execution_date = attr.get('execution_date')
    url = url_for(
        'Airflow.graph',
        dag_id=dag_id,
        execution_date=execution_date)
    return Markup(
        '<a href="{}">{}</a>'.format(url, dag_id))

def dag_run_link(attr):
    dag_id = bleach.clean(attr.get('dag_id'))
    run_id = bleach.clean(attr.get('run_id'))
    execution_date = attr.get('execution_date')
    url = url_for(
        'Airflow.graph',
        dag_id=dag_id,
        run_id=run_id,
        execution_date=execution_date)
    return Markup(
        '<a href="{url}">{run_id}</a>'.format(**locals()))

def pygment_html_render(s, lexer=lexers.TextLexer):
    return highlight(
        s,
        lexer(),
        HtmlFormatter(linenos=True),
    )

def render(obj, lexer):
    out = ""
    if isinstance(obj, basestring):
        out += pygment_html_render(obj, lexer)
    elif isinstance(obj, (tuple, list)):
        for i, s in enumerate(obj):
            out += "<div>List item #{}</div>".format(i)
            out += "<div>" + pygment_html_render(s, lexer) + "</div>"
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out += '<div>Dict item "{}"</div>'.format(k)
            out += "<div>" + pygment_html_render(v, lexer) + "</div>"
    return out


def wrapped_markdown(s):
    return '<div class="rich_doc">' + markdown.markdown(s) + "</div>"

attr_renderer = {
    'bash_command': lambda x: render(x, lexers.BashLexer),
    'hql': lambda x: render(x, lexers.SqlLexer),
    'sql': lambda x: render(x, lexers.SqlLexer),
    'doc': lambda x: render(x, lexers.TextLexer),
    'doc_json': lambda x: render(x, lexers.JsonLexer),
    'doc_rst': lambda x: render(x, lexers.RstLexer),
    'doc_yaml': lambda x: render(x, lexers.YamlLexer),
    'doc_md': wrapped_markdown,
    'python_callable': lambda x: render(
        inspect.getsource(x), lexers.PythonLexer),
}

def recurse_tasks(tasks, task_ids, dag_ids, task_id_to_dag):
    if isinstance(tasks, list):
        for task in tasks:
            recurse_tasks(task, task_ids, dag_ids, task_id_to_dag)
        return
    if isinstance(tasks, SubDagOperator):
        subtasks = tasks.subdag.tasks
        dag_ids.append(tasks.subdag.dag_id)
        for subtask in subtasks:
            if subtask.task_id not in task_ids:
                task_ids.append(subtask.task_id)
                task_id_to_dag[subtask.task_id] = tasks.subdag
        recurse_tasks(subtasks, task_ids, dag_ids, task_id_to_dag)
    if isinstance(tasks, BaseOperator):
        task_id_to_dag[tasks.task_id] = tasks.dag


def get_chart_height(dag):
    """
    TODO(aoen): See [AIRFLOW-1263] We use the number of tasks in the DAG as a heuristic to
    approximate the size of generated chart (otherwise the charts are tiny and unreadable
    when DAGs have a large number of tasks). Ideally nvd3 should allow for dynamic-height
    charts, that is charts that take up space based on the size of the components within.
    """
    return 600 + len(dag.tasks) * 10

######################################################################################
#                                    BaseViews
######################################################################################

class AirflowBaseView(BaseView):

    def render(self, template, **context):
        return render_template(template,
                               base_template=appbuilder.base_template,
                               appbuilder=appbuilder,
                               **context)


class Airflow(AirflowBaseView):
    route_base = ''

    chart_mapping = dict((
        ('line', 'lineChart'),
        ('spline', 'lineChart'),
        ('bar', 'multiBarChart'),
        ('column', 'multiBarChart'),
        ('area', 'stackedAreaChart'),
        ('stacked_area', 'stackedAreaChart'),
        ('percent_area', 'stackedAreaChart'),
        ('datatable', 'datatable'),
    ))

    def is_visible(self):
        return False

    @expose('/chart_data')
    @has_access
    @wwwutils.gzipped
    # @cache.cached(timeout=3600, key_prefix=wwwutils.make_cache_key)
    def chart_data(self):
        from airflow import macros
        import pandas as pd
        session = settings.Session()
        chart_id = request.args.get('chart_id')
        csv = request.args.get('csv') == "true"
        chart = session.query(models.Chart).filter_by(id=chart_id).first()
        db = session.query(
            models.Connection).filter_by(conn_id=chart.conn_id).first()
        session.expunge_all()
        session.commit()
        session.close()

        payload = {
            "state": "ERROR",
            "error": ""
        }

        # Processing templated fields
        try:
            args = ast.literal_eval(chart.default_params)
            if type(args) is not type(dict()):
                raise AirflowException('Not a dict')
        except:
            args = {}
            payload['error'] += (
                "Default params is not valid, string has to evaluate as "
                "a Python dictionary. ")

        request_dict = {k: request.args.get(k) for k in request.args}
        args.update(request_dict)
        args['macros'] = macros
        sandbox = ImmutableSandboxedEnvironment()
        sql = sandbox.from_string(chart.sql).render(**args)
        label = sandbox.from_string(chart.label).render(**args)
        payload['sql_html'] = Markup(highlight(
            sql,
            lexers.SqlLexer(),  # Lexer call
            HtmlFormatter(noclasses=True))
        )
        payload['label'] = label

        pd.set_option('display.max_colwidth', 100)
        hook = db.get_hook()
        try:
            df = hook.get_pandas_df(
                wwwutils.limit_sql(sql, CHART_LIMIT, conn_type=db.conn_type))
            df = df.fillna(0)
        except Exception as e:
            payload['error'] += "SQL execution failed. Details: " + str(e)

        if csv:
            return Response(
                response=df.to_csv(index=False),
                status=200,
                mimetype="application/text")

        if not payload['error'] and len(df) == CHART_LIMIT:
            payload['warning'] = (
                "Data has been truncated to {0}"
                " rows. Expect incomplete results.").format(CHART_LIMIT)

        if not payload['error'] and len(df) == 0:
            payload['error'] += "Empty result set. "
        elif (
                        not payload['error'] and
                            chart.sql_layout == 'series' and
                        chart.chart_type != "datatable" and
                    len(df.columns) < 3):
            payload['error'] += "SQL needs to return at least 3 columns. "
        elif (
                    not payload['error'] and
                        chart.sql_layout == 'columns' and
                    len(df.columns) < 2):
            payload['error'] += "SQL needs to return at least 2 columns. "
        elif not payload['error']:
            import numpy as np
            chart_type = chart.chart_type

            data = None
            if chart.show_datatable or chart_type == "datatable":
                data = df.to_dict(orient="split")
                data['columns'] = [{'title': c} for c in data['columns']]
                payload['data'] = data

            # Trying to convert time to something Highcharts likes
            x_col = 1 if chart.sql_layout == 'series' else 0
            if chart.x_is_date:
                try:
                    # From string to datetime
                    df[df.columns[x_col]] = pd.to_datetime(
                        df[df.columns[x_col]])
                    df[df.columns[x_col]] = df[df.columns[x_col]].apply(
                        lambda x: int(x.strftime("%s")) * 1000)
                except Exception as e:
                    payload['error'] = "Time conversion failed"

            if chart_type == 'datatable':
                payload['state'] = 'SUCCESS'
                return wwwutils.json_response(payload)
            else:
                if chart.sql_layout == 'series':
                    # User provides columns (series, x, y)
                    xaxis_label = df.columns[1]
                    yaxis_label = df.columns[2]
                    df[df.columns[2]] = df[df.columns[2]].astype(np.float)
                    df = df.pivot_table(
                        index=df.columns[1],
                        columns=df.columns[0],
                        values=df.columns[2], aggfunc=np.sum)
                else:
                    # User provides columns (x, y, metric1, metric2, ...)
                    xaxis_label = df.columns[0]
                    yaxis_label = 'y'
                    df.index = df[df.columns[0]]
                    df = df.sort(df.columns[0])
                    del df[df.columns[0]]
                    for col in df.columns:
                        df[col] = df[col].astype(np.float)

                df = df.fillna(0)
                NVd3ChartClass = self.chart_mapping.get(chart.chart_type)
                NVd3ChartClass = getattr(nvd3, NVd3ChartClass)
                nvd3_chart = NVd3ChartClass(x_is_date=chart.x_is_date)

                for col in df.columns:
                    nvd3_chart.add_serie(name=col, y=df[col].tolist(), x=df[col].index.tolist())
                try:
                    nvd3_chart.buildcontent()
                    payload['chart_type'] = nvd3_chart.__class__.__name__
                    payload['htmlcontent'] = nvd3_chart.htmlcontent
                except Exception as e:
                    payload['error'] = str(e)

            payload['state'] = 'SUCCESS'
            payload['request_dict'] = request_dict
        return wwwutils.json_response(payload)

    @expose('/chart')
    @has_access
    def chart(self):
        session = settings.Session()
        chart_id = request.args.get('chart_id')
        embed = request.args.get('embed')
        chart = session.query(models.Chart).filter_by(id=chart_id).first()
        session.expunge_all()
        session.commit()
        session.close()

        NVd3ChartClass = self.chart_mapping.get(chart.chart_type)
        if not NVd3ChartClass:
            flash(
                "Not supported anymore as the license was incompatible, "
                "sorry",
                "danger")
            redirect('/chart/')

        sql = ""
        if chart.show_sql:
            sql = Markup(highlight(
                chart.sql,
                lexers.SqlLexer(),  # Lexer call
                HtmlFormatter(noclasses=True))
            )
        return self.render(
            'airflow/nvd3.html',
            chart=chart,
            title="Airflow - Chart",
            sql=sql,
            label=chart.label,
            embed=embed)

    @expose('/dag_stats')
    @has_access
    def dag_stats(self):
        ds = models.DagStat
        session = Session()

        ds.update()

        qry = (
            session.query(ds.dag_id, ds.state, ds.count)
        )

        data = {}
        for dag_id, state, count in qry:
            if dag_id not in data:
                data[dag_id] = {}
            data[dag_id][state] = count

        payload = {}
        for dag in dagbag.dags.values():
            payload[dag.safe_dag_id] = []
            for state in State.dag_states:
                try:
                    count = data[dag.dag_id][state]
                except Exception:
                    count = 0
                d = {
                    'state': state,
                    'count': count,
                    'dag_id': dag.dag_id,
                    'color': State.color(state)
                }
                payload[dag.safe_dag_id].append(d)
        return wwwutils.json_response(payload)

    @expose('/task_stats')
    @has_access
    def task_stats(self):
        TI = models.TaskInstance
        DagRun = models.DagRun
        Dag = models.DagModel
        session = Session()

        LastDagRun = (
            session.query(DagRun.dag_id, sqla.func.max(DagRun.execution_date).label('execution_date'))
                .join(Dag, Dag.dag_id == DagRun.dag_id)
                .filter(DagRun.state != State.RUNNING)
                .filter(Dag.is_active == True)
                .group_by(DagRun.dag_id)
                .subquery('last_dag_run')
        )
        RunningDagRun = (
            session.query(DagRun.dag_id, DagRun.execution_date)
                .join(Dag, Dag.dag_id == DagRun.dag_id)
                .filter(DagRun.state == State.RUNNING)
                .filter(Dag.is_active == True)
                .subquery('running_dag_run')
        )

        # Select all task_instances from active dag_runs.
        # If no dag_run is active, return task instances from most recent dag_run.
        LastTI = (
            session.query(TI.dag_id.label('dag_id'), TI.state.label('state'))
                .join(LastDagRun, and_(
                LastDagRun.c.dag_id == TI.dag_id,
                LastDagRun.c.execution_date == TI.execution_date))
        )
        RunningTI = (
            session.query(TI.dag_id.label('dag_id'), TI.state.label('state'))
                .join(RunningDagRun, and_(
                RunningDagRun.c.dag_id == TI.dag_id,
                RunningDagRun.c.execution_date == TI.execution_date))
        )

        UnionTI = union_all(LastTI, RunningTI).alias('union_ti')
        qry = (
            session.query(UnionTI.c.dag_id, UnionTI.c.state, sqla.func.count())
                .group_by(UnionTI.c.dag_id, UnionTI.c.state)
        )

        data = {}
        for dag_id, state, count in qry:
            if dag_id not in data:
                data[dag_id] = {}
            data[dag_id][state] = count
        session.commit()
        session.close()

        payload = {}
        for dag in dagbag.dags.values():
            payload[dag.safe_dag_id] = []
            for state in State.task_states:
                try:
                    count = data[dag.dag_id][state]
                except:
                    count = 0
                d = {
                    'state': state,
                    'count': count,
                    'dag_id': dag.dag_id,
                    'color': State.color(state)
                }
                payload[dag.safe_dag_id].append(d)
        return wwwutils.json_response(payload)

    @expose('/code')
    @has_access
    def code(self):
        dag_id = request.args.get('dag_id')
        dag = dagbag.get_dag(dag_id)
        title = dag_id
        try:
            with open(dag.fileloc, 'r') as f:
                code = f.read()
            html_code = highlight(
                code, lexers.PythonLexer(), HtmlFormatter(linenos=True))
        except IOError as e:
            html_code = str(e)

        return self.render(
            'airflow/dag_code.html', html_code=html_code, dag=dag, title=title,
            root=request.args.get('root'),
            demo_mode=conf.getboolean('webserver', 'demo_mode'))

    @expose('/dag_details')
    @has_access
    def dag_details(self):
        dag_id = request.args.get('dag_id')
        dag = dagbag.get_dag(dag_id)
        title = "DAG details"

        session = settings.Session()
        TI = models.TaskInstance
        states = (
            session.query(TI.state, sqla.func.count(TI.dag_id))
                .filter(TI.dag_id == dag_id)
                .group_by(TI.state)
                .all()
        )
        return self.render(
            'airflow/dag_details.html',
            dag=dag, title=title, states=states, State=State)

    @appbuilder.app.errorhandler(404)
    def circles(self):
        return render_template(
            'airflow/circles.html', hostname=socket.getfqdn()), 404

    @appbuilder.app.errorhandler(500)
    def show_traceback(self):
        from airflow.utils import asciiart as ascii_
        return render_template(
            'airflow/traceback.html',
            hostname=socket.getfqdn(),
            nukular=ascii_.nukular,
            info=traceback.format_exc()), 500

    @expose('/noaccess')
    @has_access
    def noaccess(self):
        return self.render('airflow/noaccess.html')

    @expose('/pickle_info')
    @has_access
    def pickle_info(self):
        d = {}
        dag_id = request.args.get('dag_id')
        dags = [dagbag.dags.get(dag_id)] if dag_id else dagbag.dags.values()
        for dag in dags:
            if not dag.is_subdag:
                d[dag.dag_id] = dag.pickle_info()
        return wwwutils.json_response(d)

    @expose('/rendered')
    @has_access
    @wwwutils.action_logging
    def rendered(self):
        dag_id = request.args.get('dag_id')
        task_id = request.args.get('task_id')
        execution_date = request.args.get('execution_date')
        dttm = dateutil.parser.parse(execution_date)
        form = DateTimeForm(data={'execution_date': dttm})
        dag = dagbag.get_dag(dag_id)
        task = copy.copy(dag.get_task(task_id))
        ti = models.TaskInstance(task=task, execution_date=dttm)
        try:
            ti.render_templates()
        except Exception as e:
            flash("Error rendering template: " + str(e), "error")
        title = "Rendered Template"
        html_dict = {}
        for template_field in task.__class__.template_fields:
            content = getattr(task, template_field)
            if template_field in attr_renderer:
                html_dict[template_field] = attr_renderer[template_field](content)
            else:
                html_dict[template_field] = (
                    "<pre><code>" + str(content) + "</pre></code>")

        return self.render(
            'airflow/ti_code.html',
            html_dict=html_dict,
            dag=dag,
            task_id=task_id,
            execution_date=execution_date,
            form=form,
            title=title, )

    @expose('/log')
    @has_access
    @wwwutils.action_logging
    def log(self):
        dag_id = request.args.get('dag_id')
        task_id = request.args.get('task_id')
        execution_date = request.args.get('execution_date')
        dttm = dateutil.parser.parse(execution_date)
        form = DateTimeForm(data={'execution_date': dttm})
        dag = dagbag.get_dag(dag_id)
        session = Session()
        ti = session.query(models.TaskInstance).filter(
            models.TaskInstance.dag_id == dag_id,
            models.TaskInstance.task_id == task_id,
            models.TaskInstance.execution_date == dttm).first()
        if ti is None:
            logs = ["*** Task instance did not exist in the DB\n"]
        else:
            logger = logging.getLogger('airflow.task')
            task_log_reader = conf.get('core', 'task_log_reader')
            handler = next((handler for handler in logger.handlers
                            if handler.name == task_log_reader), None)
            try:
                ti.task = dag.get_task(ti.task_id)
                logs = handler.read(ti)
            except AttributeError as e:
                logs = ["Task log handler {} does not support read logs.\n{}\n" \
                            .format(task_log_reader, str(e))]

        for i, log in enumerate(logs):
            if PY2 and not isinstance(log, unicode):
                logs[i] = log.decode('utf-8')

        return self.render(
            'airflow/ti_log.html',
            logs=logs, dag=dag, title="Log by attempts", task_id=task_id,
            execution_date=execution_date, form=form)

    @expose('/task')
    @has_access
    @wwwutils.action_logging
    def task(self):
        TI = models.TaskInstance

        dag_id = request.args.get('dag_id')
        task_id = request.args.get('task_id')
        # Carrying execution_date through, even though it's irrelevant for
        # this context
        execution_date = request.args.get('execution_date')
        dttm = dateutil.parser.parse(execution_date)
        form = DateTimeForm(data={'execution_date': dttm})
        dag = dagbag.get_dag(dag_id)

        if not dag or task_id not in dag.task_ids:
            flash(
                "Task [{}.{}] doesn't seem to exist"
                " at the moment".format(dag_id, task_id),
                "error")
            return redirect('/')
        task = copy.copy(dag.get_task(task_id))
        task.resolve_template_files()
        ti = TI(task=task, execution_date=dttm)
        ti.refresh_from_db()

        ti_attrs = []
        for attr_name in dir(ti):
            if not attr_name.startswith('_'):
                attr = getattr(ti, attr_name)
                if type(attr) != type(self.task):
                    ti_attrs.append((attr_name, str(attr)))

        task_attrs = []
        for attr_name in dir(task):
            if not attr_name.startswith('_'):
                attr = getattr(task, attr_name)
                if type(attr) != type(self.task) and \
                        attr_name not in attr_renderer:
                    task_attrs.append((attr_name, str(attr)))

        # Color coding the special attributes that are code
        special_attrs_rendered = {}
        for attr_name in attr_renderer:
            if hasattr(task, attr_name):
                source = getattr(task, attr_name)
                special_attrs_rendered[attr_name] = attr_renderer[attr_name](source)

        no_failed_deps_result = [(
            "Unknown",
            dedent("""\
            All dependencies are met but the task instance is not running. In most cases this just means that the task will probably be scheduled soon unless:<br/>
            - The scheduler is down or under heavy load<br/>
            {}
            <br/>
            If this task instance does not start soon please contact your Airflow administrator for assistance."""
                .format(
                "- This task instance already ran and had it's state changed manually (e.g. cleared in the UI)<br/>"
                if ti.state == State.NONE else "")))]

        # Use the scheduler's context to figure out which dependencies are not met
        dep_context = DepContext(SCHEDULER_DEPS)
        failed_dep_reasons = [(dep.dep_name, dep.reason) for dep in
                              ti.get_failed_dep_statuses(
                                  dep_context=dep_context)]

        title = "Task Instance Details"
        return self.render(
            'airflow/task.html',
            task_attrs=task_attrs,
            ti_attrs=ti_attrs,
            failed_dep_reasons=failed_dep_reasons or no_failed_deps_result,
            task_id=task_id,
            execution_date=execution_date,
            special_attrs_rendered=special_attrs_rendered,
            form=form,
            dag=dag, title=title)

    @expose('/xcom')
    @has_access
    @wwwutils.action_logging
    def xcom(self):
        dag_id = request.args.get('dag_id')
        task_id = request.args.get('task_id')
        # Carrying execution_date through, even though it's irrelevant for
        # this context
        execution_date = request.args.get('execution_date')
        dttm = dateutil.parser.parse(execution_date)
        form = DateTimeForm(data={'execution_date': dttm})
        dag = dagbag.get_dag(dag_id)
        if not dag or task_id not in dag.task_ids:
            flash(
                "Task [{}.{}] doesn't seem to exist"
                " at the moment".format(dag_id, task_id),
                "error")
            return redirect('/')

        session = Session()
        xcomlist = session.query(XCom).filter(
            XCom.dag_id == dag_id, XCom.task_id == task_id,
            XCom.execution_date == dttm).all()

        attributes = []
        for xcom in xcomlist:
            if not xcom.key.startswith('_'):
                attributes.append((xcom.key, xcom.value))

        title = "XCom"
        return self.render(
            'airflow/xcom.html',
            attributes=attributes,
            task_id=task_id,
            execution_date=execution_date,
            form=form,
            dag=dag, title=title)

    @expose('/run')
    @has_access
    @wwwutils.action_logging
    @wwwutils.notify_owner
    def run(self):
        dag_id = request.args.get('dag_id')
        task_id = request.args.get('task_id')
        origin = request.args.get('origin')
        dag = dagbag.get_dag(dag_id)
        task = dag.get_task(task_id)

        execution_date = request.args.get('execution_date')
        execution_date = dateutil.parser.parse(execution_date)
        ignore_all_deps = request.args.get('ignore_all_deps') == "true"
        ignore_task_deps = request.args.get('ignore_task_deps') == "true"
        ignore_ti_state = request.args.get('ignore_ti_state') == "true"

        try:
            from airflow.executors import GetDefaultExecutor
            from airflow.executors.celery_executor import CeleryExecutor
            executor = GetDefaultExecutor()
            if not isinstance(executor, CeleryExecutor):
                flash("Only works with the CeleryExecutor, sorry", "error")
                return redirect(origin)
        except ImportError:
            # in case CeleryExecutor cannot be imported it is not active either
            flash("Only works with the CeleryExecutor, sorry", "error")
            return redirect(origin)

        ti = models.TaskInstance(task=task, execution_date=execution_date)
        ti.refresh_from_db()

        # Make sure the task instance can be queued
        dep_context = DepContext(
            deps=QUEUE_DEPS,
            ignore_all_deps=ignore_all_deps,
            ignore_task_deps=ignore_task_deps,
            ignore_ti_state=ignore_ti_state)
        failed_deps = list(ti.get_failed_dep_statuses(dep_context=dep_context))
        if failed_deps:
            failed_deps_str = ", ".join(
                ["{}: {}".format(dep.dep_name, dep.reason) for dep in failed_deps])
            flash("Could not queue task instance for execution, dependencies not met: "
                  "{}".format(failed_deps_str),
                  "error")
            return redirect(origin)

        executor.start()
        executor.queue_task_instance(
            ti,
            ignore_all_deps=ignore_all_deps,
            ignore_task_deps=ignore_task_deps,
            ignore_ti_state=ignore_ti_state)
        executor.heartbeat()
        flash(
            "Sent {} to the message queue, "
            "it should start any moment now.".format(ti))
        return redirect(origin)

    @expose('/trigger')
    @has_access
    @wwwutils.action_logging
    @wwwutils.notify_owner
    def trigger(self):
        dag_id = request.args.get('dag_id')
        origin = request.args.get('origin') or "/"
        dag = dagbag.get_dag(dag_id)

        if not dag:
            flash("Cannot find dag {}".format(dag_id))
            return redirect(origin)

        execution_date = datetime.utcnow()
        run_id = "manual__{0}".format(execution_date.isoformat())

        dr = DagRun.find(dag_id=dag_id, run_id=run_id)
        if dr:
            flash("This run_id {} already exists".format(run_id))
            return redirect(origin)

        run_conf = {}

        dag.create_dagrun(
            run_id=run_id,
            execution_date=execution_date,
            state=State.RUNNING,
            conf=run_conf,
            external_trigger=True
        )

        flash(
            "Triggered {}, "
            "it should start any moment now.".format(dag_id))
        return redirect(origin)

    def _clear_dag_tis(self, dag, start_date, end_date, origin,
                       recursive=False, confirmed=False):
        if confirmed:
            count = dag.clear(
                start_date=start_date,
                end_date=end_date,
                include_subdags=recursive)

            flash("{0} task instances have been cleared".format(count))
            return redirect(origin)

        tis = dag.clear(
            start_date=start_date,
            end_date=end_date,
            include_subdags=recursive,
            dry_run=True)
        if not tis:
            flash("No task instances to clear", 'error')
            response = redirect(origin)
        else:
            details = "\n".join([str(t) for t in tis])

            response = self.render(
                'airflow/confirm.html',
                message=("Here's the list of task instances you are about "
                         "to clear:"),
                details=details)

        return response

    @expose('/clear')
    @has_access
    @wwwutils.action_logging
    @wwwutils.notify_owner
    def clear(self):
        dag_id = request.args.get('dag_id')
        task_id = request.args.get('task_id')
        origin = request.args.get('origin')
        dag = dagbag.get_dag(dag_id)

        execution_date = request.args.get('execution_date')
        execution_date = dateutil.parser.parse(execution_date)
        confirmed = request.args.get('confirmed') == "true"
        upstream = request.args.get('upstream') == "true"
        downstream = request.args.get('downstream') == "true"
        future = request.args.get('future') == "true"
        past = request.args.get('past') == "true"
        recursive = request.args.get('recursive') == "true"

        dag = dag.sub_dag(
            task_regex=r"^{0}$".format(task_id),
            include_downstream=downstream,
            include_upstream=upstream)

        end_date = execution_date if not future else None
        start_date = execution_date if not past else None

        return self._clear_dag_tis(dag, start_date, end_date, origin,
                                   recursive=recursive, confirmed=confirmed)

    @expose('/dagrun_clear')
    @has_access
    @wwwutils.action_logging
    @wwwutils.notify_owner
    def dagrun_clear(self):
        dag_id = request.args.get('dag_id')
        task_id = request.args.get('task_id')
        origin = request.args.get('origin')
        execution_date = request.args.get('execution_date')
        confirmed = request.args.get('confirmed') == "true"

        dag = dagbag.get_dag(dag_id)
        execution_date = dateutil.parser.parse(execution_date)
        start_date = execution_date
        end_date = execution_date

        return self._clear_dag_tis(dag, start_date, end_date, origin,
                                   recursive=True, confirmed=confirmed)

    @expose('/blocked')
    @has_access
    def blocked(self):
        session = settings.Session()
        DR = models.DagRun
        dags = (
            session.query(DR.dag_id, sqla.func.count(DR.id))
                .filter(DR.state == State.RUNNING)
                .group_by(DR.dag_id)
                .all()
        )
        payload = []
        for dag_id, active_dag_runs in dags:
            max_active_runs = 0
            if dag_id in dagbag.dags:
                max_active_runs = dagbag.dags[dag_id].max_active_runs
            payload.append({
                'dag_id': dag_id,
                'active_dag_run': active_dag_runs,
                'max_active_runs': max_active_runs,
            })
        return wwwutils.json_response(payload)

    @expose('/dagrun_success')
    @has_access
    @wwwutils.action_logging
    @wwwutils.notify_owner
    def dagrun_success(self):
        dag_id = request.args.get('dag_id')
        execution_date = request.args.get('execution_date')
        confirmed = request.args.get('confirmed') == 'true'
        origin = request.args.get('origin')

        if not execution_date:
            flash('Invalid execution date', 'error')
            return redirect(origin)

        execution_date = dateutil.parser.parse(execution_date)
        dag = dagbag.get_dag(dag_id)

        if not dag:
            flash('Cannot find DAG: {}'.format(dag_id), 'error')
            return redirect(origin)

        new_dag_state = set_dag_run_state(dag, execution_date, state=State.SUCCESS,
                                          commit=confirmed)

        if confirmed:
            flash('Marked success on {} task instances'.format(len(new_dag_state)))
            return redirect(origin)

        else:
            details = '\n'.join([str(t) for t in new_dag_state])

            response = self.render('airflow/confirm.html',
                                   message=("Here's the list of task instances you are "
                                            "about to mark as successful:"),
                                   details=details)

            return response

    @expose('/success')
    @has_access
    @wwwutils.action_logging
    @wwwutils.notify_owner
    def success(self):
        dag_id = request.args.get('dag_id')
        task_id = request.args.get('task_id')
        origin = request.args.get('origin')
        dag = dagbag.get_dag(dag_id)
        task = dag.get_task(task_id)
        task.dag = dag

        execution_date = request.args.get('execution_date')
        execution_date = dateutil.parser.parse(execution_date)
        confirmed = request.args.get('confirmed') == "true"
        upstream = request.args.get('upstream') == "true"
        downstream = request.args.get('downstream') == "true"
        future = request.args.get('future') == "true"
        past = request.args.get('past') == "true"

        if not dag:
            flash("Cannot find DAG: {}".format(dag_id))
            return redirect(origin)

        if not task:
            flash("Cannot find task {} in DAG {}".format(task_id, dag.dag_id))
            return redirect(origin)

        from airflow.api.common.experimental.mark_tasks import set_state

        if confirmed:
            altered = set_state(task=task, execution_date=execution_date,
                                upstream=upstream, downstream=downstream,
                                future=future, past=past, state=State.SUCCESS,
                                commit=True)

            flash("Marked success on {} task instances".format(len(altered)))
            return redirect(origin)

        to_be_altered = set_state(task=task, execution_date=execution_date,
                                  upstream=upstream, downstream=downstream,
                                  future=future, past=past, state=State.SUCCESS,
                                  commit=False)

        details = "\n".join([str(t) for t in to_be_altered])

        response = self.render("airflow/confirm.html",
                               message=("Here's the list of task instances you are "
                                        "about to mark as successful:"),
                               details=details)

        return response

    @expose('/tree')
    @has_access
    @wwwutils.gzipped
    @wwwutils.action_logging
    def tree(self):
        dag_id = request.args.get('dag_id')
        blur = conf.getboolean('webserver', 'demo_mode')
        dag = dagbag.get_dag(dag_id)
        root = request.args.get('root')
        if root:
            dag = dag.sub_dag(
                task_regex=root,
                include_downstream=False,
                include_upstream=True)

        session = settings.Session()

        base_date = request.args.get('base_date')
        num_runs = request.args.get('num_runs')
        num_runs = int(num_runs) if num_runs else 25

        if base_date:
            base_date = dateutil.parser.parse(base_date)
        else:
            base_date = dag.latest_execution_date or datetime.utcnow()

        dates = dag.date_range(base_date, num=-abs(num_runs))
        min_date = dates[0] if dates else datetime(2000, 1, 1)

        DR = models.DagRun
        dag_runs = (
            session.query(DR)
                .filter(
                DR.dag_id == dag.dag_id,
                DR.execution_date <= base_date,
                DR.execution_date >= min_date)
                .all()
        )
        dag_runs = {
            dr.execution_date: alchemy_to_dict(dr) for dr in dag_runs}

        dates = sorted(list(dag_runs.keys()))
        max_date = max(dates) if dates else None

        tis = dag.get_task_instances(
            session, start_date=min_date, end_date=base_date)
        task_instances = {}
        for ti in tis:
            tid = alchemy_to_dict(ti)
            dr = dag_runs.get(ti.execution_date)
            tid['external_trigger'] = dr['external_trigger'] if dr else False
            task_instances[(ti.task_id, ti.execution_date)] = tid

        expanded = []
        # The default recursion traces every path so that tree view has full
        # expand/collapse functionality. After 5,000 nodes we stop and fall
        # back on a quick DFS search for performance. See PR #320.
        node_count = [0]
        node_limit = 5000 / max(1, len(dag.roots))

        def recurse_nodes(task, visited):
            visited.add(task)
            node_count[0] += 1

            children = [
                recurse_nodes(t, visited) for t in task.upstream_list
                if node_count[0] < node_limit or t not in visited]

            # D3 tree uses children vs _children to define what is
            # expanded or not. The following block makes it such that
            # repeated nodes are collapsed by default.
            children_key = 'children'
            if task.task_id not in expanded:
                expanded.append(task.task_id)
            elif children:
                children_key = "_children"

            def set_duration(tid):
                if (isinstance(tid, dict) and tid.get("state") == State.RUNNING and
                            tid["start_date"] is not None):
                    d = datetime.utcnow() - dateutil.parser.parse(tid["start_date"])
                    tid["duration"] = d.total_seconds()
                return tid

            return {
                'name': task.task_id,
                'instances': [
                    set_duration(task_instances.get((task.task_id, d))) or {
                        'execution_date': d.isoformat(),
                        'task_id': task.task_id
                    }
                    for d in dates],
                children_key: children,
                'num_dep': len(task.upstream_list),
                'operator': task.task_type,
                'retries': task.retries,
                'owner': task.owner,
                'start_date': task.start_date,
                'end_date': task.end_date,
                'depends_on_past': task.depends_on_past,
                'ui_color': task.ui_color,
            }

        data = {
            'name': '[DAG]',
            'children': [recurse_nodes(t, set()) for t in dag.roots],
            'instances': [
                dag_runs.get(d) or {'execution_date': d.isoformat()}
                for d in dates],
        }

        data = json.dumps(data, indent=4, default=json_ser)
        session.commit()
        session.close()

        form = DateTimeWithNumRunsForm(data={'base_date': max_date,
                                             'num_runs': num_runs})
        return self.render(
            'airflow/tree.html',
            operators=sorted(
                list(set([op.__class__ for op in dag.tasks])),
                key=lambda x: x.__name__
            ),
            root=root,
            form=form,
            dag=dag, data=data, blur=blur)

    @expose('/graph')
    @has_access
    @wwwutils.gzipped
    @wwwutils.action_logging
    def graph(self):
        session = settings.Session()
        dag_id = request.args.get('dag_id')
        blur = conf.getboolean('webserver', 'demo_mode')
        dag = dagbag.get_dag(dag_id)
        if dag_id not in dagbag.dags:
            flash('DAG "{0}" seems to be missing.'.format(dag_id), "error")
            return redirect('/')

        root = request.args.get('root')
        if root:
            dag = dag.sub_dag(
                task_regex=root,
                include_upstream=True,
                include_downstream=False)

        arrange = request.args.get('arrange', dag.orientation)

        nodes = []
        edges = []
        for task in dag.tasks:
            nodes.append({
                'id': task.task_id,
                'value': {
                    'label': task.task_id,
                    'labelStyle': "fill:{0};".format(task.ui_fgcolor),
                    'style': "fill:{0};".format(task.ui_color),
                }
            })

        def get_upstream(task):
            for t in task.upstream_list:
                edge = {
                    'u': t.task_id,
                    'v': task.task_id,
                }
                if edge not in edges:
                    edges.append(edge)
                    get_upstream(t)

        for t in dag.roots:
            get_upstream(t)

        dttm = request.args.get('execution_date')
        if dttm:
            dttm = dateutil.parser.parse(dttm)
        else:
            dttm = dag.latest_execution_date or datetime.utcnow().date()

        DR = models.DagRun
        drs = (
            session.query(DR)
                .filter_by(dag_id=dag_id)
                .order_by(desc(DR.execution_date)).all()
        )
        dr_choices = []
        dr_state = None
        for dr in drs:
            dr_choices.append((dr.execution_date.isoformat(), dr.run_id))
            if dttm == dr.execution_date:
                dr_state = dr.state

        class GraphForm(Form):
            execution_date = SelectField("DAG run", choices=dr_choices)
            arrange = SelectField("Layout", choices=(
                ('LR', "Left->Right"),
                ('RL', "Right->Left"),
                ('TB', "Top->Bottom"),
                ('BT', "Bottom->Top"),
            ))

        form = GraphForm(
            data={'execution_date': dttm.isoformat(), 'arrange': arrange})

        task_instances = {
            ti.task_id: alchemy_to_dict(ti)
            for ti in dag.get_task_instances(session, dttm, dttm)}
        tasks = {
            t.task_id: {
                'dag_id': t.dag_id,
                'task_type': t.task_type,
            }
            for t in dag.tasks}
        if not tasks:
            flash("No tasks found", "error")
        session.commit()
        session.close()
        doc_md = markdown.markdown(dag.doc_md) if hasattr(dag, 'doc_md') and dag.doc_md else ''

        return self.render(
            'airflow/graph.html',
            dag=dag,
            form=form,
            width=request.args.get('width', "100%"),
            height=request.args.get('height', "800"),
            execution_date=dttm.isoformat(),
            state_token=state_token(dr_state),
            doc_md=doc_md,
            arrange=arrange,
            operators=sorted(
                list(set([op.__class__ for op in dag.tasks])),
                key=lambda x: x.__name__
            ),
            blur=blur,
            root=root or '',
            task_instances=json.dumps(task_instances, indent=2),
            tasks=json.dumps(tasks, indent=2),
            nodes=json.dumps(nodes, indent=2),
            edges=json.dumps(edges, indent=2), )

    @expose('/duration')
    @has_access
    @wwwutils.action_logging
    def duration(self):
        session = settings.Session()
        dag_id = request.args.get('dag_id')
        dag = dagbag.get_dag(dag_id)
        base_date = request.args.get('base_date')
        num_runs = request.args.get('num_runs')
        num_runs = int(num_runs) if num_runs else 25

        if base_date:
            base_date = dateutil.parser.parse(base_date)
        else:
            base_date = dag.latest_execution_date or datetime.utcnow()

        dates = dag.date_range(base_date, num=-abs(num_runs))
        min_date = dates[0] if dates else datetime(2000, 1, 1)

        root = request.args.get('root')
        if root:
            dag = dag.sub_dag(
                task_regex=root,
                include_upstream=True,
                include_downstream=False)

        chart_height = get_chart_height(dag)
        chart = nvd3.lineChart(
            name="lineChart", x_is_date=True, height=chart_height, width="1200")
        cum_chart = nvd3.lineChart(
            name="cumLineChart", x_is_date=True, height=chart_height, width="1200")

        y = defaultdict(list)
        x = defaultdict(list)
        cum_y = defaultdict(list)

        tis = dag.get_task_instances(
            session, start_date=min_date, end_date=base_date)
        TF = models.TaskFail
        ti_fails = (
            session
                .query(TF)
                .filter(
                TF.dag_id == dag.dag_id,
                TF.execution_date >= min_date,
                TF.execution_date <= base_date,
                TF.task_id.in_([t.task_id for t in dag.tasks]))
                .all()
        )

        fails_totals = defaultdict(int)
        for tf in ti_fails:
            dict_key = (tf.dag_id, tf.task_id, tf.execution_date)
            fails_totals[dict_key] += tf.duration

        for ti in tis:
            if ti.duration:
                dttm = wwwutils.epoch(ti.execution_date)
                x[ti.task_id].append(dttm)
                y[ti.task_id].append(float(ti.duration))
                fails_dict_key = (ti.dag_id, ti.task_id, ti.execution_date)
                fails_total = fails_totals[fails_dict_key]
                cum_y[ti.task_id].append(float(ti.duration + fails_total))

        # determine the most relevant time unit for the set of task instance
        # durations for the DAG
        y_unit = infer_time_unit([d for t in y.values() for d in t])
        cum_y_unit = infer_time_unit([d for t in cum_y.values() for d in t])
        # update the y Axis on both charts to have the correct time units
        chart.create_y_axis('yAxis', format='.02f', custom_format=False,
                            label='Duration ({})'.format(y_unit))
        cum_chart.create_y_axis('yAxis', format='.02f', custom_format=False,
                                label='Duration ({})'.format(cum_y_unit))
        for task in dag.tasks:
            if x[task.task_id]:
                chart.add_serie(name=task.task_id, x=x[task.task_id],
                                y=scale_time_units(y[task.task_id], y_unit))
                cum_chart.add_serie(name=task.task_id, x=x[task.task_id],
                                    y=scale_time_units(cum_y[task.task_id],
                                                       cum_y_unit))

        dates = sorted(list({ti.execution_date for ti in tis}))
        max_date = max([ti.execution_date for ti in tis]) if dates else None

        session.commit()
        session.close()

        form = DateTimeWithNumRunsForm(data={'base_date': max_date,
                                             'num_runs': num_runs})
        chart.buildcontent()
        cum_chart.buildcontent()
        s_index = cum_chart.htmlcontent.rfind('});')
        cum_chart.htmlcontent = (cum_chart.htmlcontent[:s_index] +
                                 "$( document ).trigger('chartload')" +
                                 cum_chart.htmlcontent[s_index:])

        return self.render(
            'airflow/duration_chart.html',
            dag=dag,
            demo_mode=conf.getboolean('webserver', 'demo_mode'),
            root=root,
            form=form,
            chart=chart.htmlcontent,
            cum_chart=cum_chart.htmlcontent
        )

    @expose('/tries')
    @has_access
    @wwwutils.action_logging
    def tries(self):
        session = settings.Session()
        dag_id = request.args.get('dag_id')
        dag = dagbag.get_dag(dag_id)
        base_date = request.args.get('base_date')
        num_runs = request.args.get('num_runs')
        num_runs = int(num_runs) if num_runs else 25

        if base_date:
            base_date = dateutil.parser.parse(base_date)
        else:
            base_date = dag.latest_execution_date or datetime.utcnow()

        dates = dag.date_range(base_date, num=-abs(num_runs))
        min_date = dates[0] if dates else datetime(2000, 1, 1)

        root = request.args.get('root')
        if root:
            dag = dag.sub_dag(
                task_regex=root,
                include_upstream=True,
                include_downstream=False)

        chart_height = get_chart_height(dag)
        chart = nvd3.lineChart(
            name="lineChart", x_is_date=True, y_axis_format='d', height=chart_height,
            width="1200")

        for task in dag.tasks:
            y = []
            x = []
            for ti in task.get_task_instances(session, start_date=min_date,
                                              end_date=base_date):
                dttm = wwwutils.epoch(ti.execution_date)
                x.append(dttm)
                y.append(ti.try_number)
            if x:
                chart.add_serie(name=task.task_id, x=x, y=y)

        tis = dag.get_task_instances(
            session, start_date=min_date, end_date=base_date)
        tries = sorted(list({ti.try_number for ti in tis}))
        max_date = max([ti.execution_date for ti in tis]) if tries else None

        session.commit()
        session.close()

        form = DateTimeWithNumRunsForm(data={'base_date': max_date,
                                             'num_runs': num_runs})

        chart.buildcontent()

        return self.render(
            'airflow/chart.html',
            dag=dag,
            demo_mode=conf.getboolean('webserver', 'demo_mode'),
            root=root,
            form=form,
            chart=chart.htmlcontent
        )

    @expose('/landing_times')
    @has_access
    @wwwutils.action_logging
    def landing_times(self):
        session = settings.Session()
        dag_id = request.args.get('dag_id')
        dag = dagbag.get_dag(dag_id)
        base_date = request.args.get('base_date')
        num_runs = request.args.get('num_runs')
        num_runs = int(num_runs) if num_runs else 25

        if base_date:
            base_date = dateutil.parser.parse(base_date)
        else:
            base_date = dag.latest_execution_date or datetime.utcnow()

        dates = dag.date_range(base_date, num=-abs(num_runs))
        min_date = dates[0] if dates else datetime(2000, 1, 1)

        root = request.args.get('root')
        if root:
            dag = dag.sub_dag(
                task_regex=root,
                include_upstream=True,
                include_downstream=False)

        chart_height = get_chart_height(dag)
        chart = nvd3.lineChart(
            name="lineChart", x_is_date=True, height=chart_height, width="1200")
        y = {}
        x = {}
        for task in dag.tasks:
            y[task.task_id] = []
            x[task.task_id] = []
            for ti in task.get_task_instances(session, start_date=min_date,
                                              end_date=base_date):
                ts = ti.execution_date
                if dag.schedule_interval and dag.following_schedule(ts):
                    ts = dag.following_schedule(ts)
                if ti.end_date:
                    dttm = wwwutils.epoch(ti.execution_date)
                    secs = (ti.end_date - ts).total_seconds()
                    x[ti.task_id].append(dttm)
                    y[ti.task_id].append(secs)

        # determine the most relevant time unit for the set of landing times
        # for the DAG
        y_unit = infer_time_unit([d for t in y.values() for d in t])
        # update the y Axis to have the correct time units
        chart.create_y_axis('yAxis', format='.02f', custom_format=False,
                            label='Landing Time ({})'.format(y_unit))
        for task in dag.tasks:
            if x[task.task_id]:
                chart.add_serie(name=task.task_id, x=x[task.task_id],
                                y=scale_time_units(y[task.task_id], y_unit))

        tis = dag.get_task_instances(
            session, start_date=min_date, end_date=base_date)
        dates = sorted(list({ti.execution_date for ti in tis}))
        max_date = max([ti.execution_date for ti in tis]) if dates else None

        session.commit()
        session.close()

        form = DateTimeWithNumRunsForm(data={'base_date': max_date,
                                             'num_runs': num_runs})
        chart.buildcontent()
        return self.render(
            'airflow/chart.html',
            dag=dag,
            chart=chart.htmlcontent,
            height=str(chart_height + 100) + "px",
            demo_mode=conf.getboolean('webserver', 'demo_mode'),
            root=root,
            form=form,
        )

    @expose('/paused', methods=['POST'])
    @has_access
    @wwwutils.action_logging
    def paused(self):
        DagModel = models.DagModel
        dag_id = request.args.get('dag_id')
        session = settings.Session()
        orm_dag = session.query(
            DagModel).filter(DagModel.dag_id == dag_id).first()
        if request.args.get('is_paused') == 'false':
            orm_dag.is_paused = True
        else:
            orm_dag.is_paused = False
        session.merge(orm_dag)
        session.commit()
        session.close()

        dagbag.get_dag(dag_id)
        return "OK"

    @expose('/refresh')
    @has_access
    @wwwutils.action_logging
    def refresh(self):
        DagModel = models.DagModel
        dag_id = request.args.get('dag_id')
        session = settings.Session()
        orm_dag = session.query(
            DagModel).filter(DagModel.dag_id == dag_id).first()

        if orm_dag:
            orm_dag.last_expired = datetime.utcnow()
            session.merge(orm_dag)
        session.commit()
        session.close()

        dagbag.get_dag(dag_id)
        flash("DAG [{}] is now fresh as a daisy".format(dag_id))
        return redirect(request.referrer)

    @expose('/refresh_all')
    @has_access
    @wwwutils.action_logging
    def refresh_all(self):
        dagbag.collect_dags(only_if_updated=False)
        flash("All DAGs are now up to date")
        return redirect('/')

    @expose('/gantt')
    @has_access
    @wwwutils.action_logging
    def gantt(self):
        session = settings.Session()
        dag_id = request.args.get('dag_id')
        dag = dagbag.get_dag(dag_id)
        demo_mode = conf.getboolean('webserver', 'demo_mode')

        root = request.args.get('root')
        if root:
            dag = dag.sub_dag(
                task_regex=root,
                include_upstream=True,
                include_downstream=False)

        dttm = request.args.get('execution_date')
        if dttm:
            dttm = dateutil.parser.parse(dttm)
        else:
            dttm = dag.latest_execution_date or datetime.utcnow().date()

        form = DateTimeForm(data={'execution_date': dttm})

        tis = [
            ti for ti in dag.get_task_instances(session, dttm, dttm)
            if ti.start_date]
        tis = sorted(tis, key=lambda ti: ti.start_date)

        tasks = []
        for ti in tis:
            end_date = ti.end_date if ti.end_date else datetime.utcnow()
            tasks.append({
                'startDate': wwwutils.epoch(ti.start_date),
                'endDate': wwwutils.epoch(end_date),
                'isoStart': ti.start_date.isoformat()[:-4],
                'isoEnd': end_date.isoformat()[:-4],
                'taskName': ti.task_id,
                'duration': "{}".format(end_date - ti.start_date)[:-4],
                'status': ti.state,
                'executionDate': ti.execution_date.isoformat(),
            })
        states = {ti.state: ti.state for ti in tis}
        data = {
            'taskNames': [ti.task_id for ti in tis],
            'tasks': tasks,
            'taskStatus': states,
            'height': len(tis) * 25 + 25,
        }

        session.commit()
        session.close()

        return self.render(
            'airflow/gantt.html',
            dag=dag,
            execution_date=dttm.isoformat(),
            form=form,
            data=json.dumps(data, indent=2),
            base_date='',
            demo_mode=demo_mode,
            root=root,
        )

    @expose('/object/task_instances')
    @has_access
    @wwwutils.action_logging
    def task_instances(self):
        session = settings.Session()
        dag_id = request.args.get('dag_id')
        dag = dagbag.get_dag(dag_id)

        dttm = request.args.get('execution_date')
        if dttm:
            dttm = dateutil.parser.parse(dttm)
        else:
            return ("Error: Invalid execution_date")

        task_instances = {
            ti.task_id: alchemy_to_dict(ti)
            for ti in dag.get_task_instances(session, dttm, dttm)}

        return json.dumps(task_instances)

    @expose('/variables/<form>', methods=["GET", "POST"])
    @has_access
    @wwwutils.action_logging
    def variables(self, form):
        try:
            if request.method == 'POST':
                data = request.json
                if data:
                    session = settings.Session()
                    var = models.Variable(key=form, val=json.dumps(data))
                    session.add(var)
                    session.commit()
                return ""
            else:
                return self.render(
                    'airflow/variables/{}.html'.format(form)
                )
        except:
            # prevent XSS
            form = escape(form)
            return ("Error: form airflow/variables/{}.html "
                    "not found.").format(form), 404

    @expose('/varimport', methods=["GET", "POST"])
    @has_access
    @wwwutils.action_logging
    def varimport(self):
        try:
            out = str(request.files['file'].read())
            d = json.loads(out)
        except Exception:
            flash("Missing file or syntax error.")
        else:
            for k, v in d.items():
                models.Variable.set(k, v, serialize_json=isinstance(v, dict))
            flash("{} variable(s) successfully updated.".format(len(d)))
        return redirect('/variable/list')


class HomeView(AirflowBaseView):
    route_base = '/'

    @expose('home')
    @has_access
    def index(self):
        session = Session()
        DM = models.DagModel

        hide_paused_dags_by_default = conf.getboolean('webserver',
                                                      'hide_paused_dags_by_default')
        show_paused_arg = request.args.get('showPaused', 'None')

        def get_int_arg(value, default=0):
            try:
                return int(value)
            except ValueError:
                return default

        arg_current_page = request.args.get('page', '0')
        arg_search_query = request.args.get('search', None)

        dags_per_page = PAGE_SIZE
        current_page = get_int_arg(arg_current_page, default=0)

        if show_paused_arg.strip().lower() == 'false':
            hide_paused = True
        elif show_paused_arg.strip().lower() == 'true':
            hide_paused = False
        else:
            hide_paused = hide_paused_dags_by_default

        # read orm_dags from the db
        sql_query = session.query(DM).filter(
            ~DM.is_subdag, DM.is_active
        )

        # optionally filter out "paused" dags
        if hide_paused:
            sql_query = sql_query.filter(~DM.is_paused)

        orm_dags = {dag.dag_id: dag for dag
                    in sql_query
                    .all()}

        import_errors = session.query(models.ImportError).all()
        for ie in import_errors:
            flash(
                "Broken DAG: [{ie.filename}] {ie.stacktrace}".format(ie=ie),
                "error")
        session.expunge_all()
        session.commit()
        session.close()

        # get a list of all non-subdag dags visible to everyone
        # optionally filter out "paused" dags
        if hide_paused:
            unfiltered_webserver_dags = [dag for dag in dagbag.dags.values() if
                                         not dag.parent_dag and not dag.is_paused]

        else:
            unfiltered_webserver_dags = [dag for dag in dagbag.dags.values() if
                                         not dag.parent_dag]

        webserver_dags = {
            dag.dag_id: dag
            for dag in unfiltered_webserver_dags
        }

        if arg_search_query:
            lower_search_query = arg_search_query.lower()
            # filter by dag_id
            webserver_dags_filtered = {
                dag_id: dag
                for dag_id, dag in webserver_dags.items()
                if (lower_search_query in dag_id.lower() or
                    lower_search_query in dag.owner.lower())
            }

            all_dag_ids = (set([dag.dag_id for dag in orm_dags.values()
                                if lower_search_query in dag.dag_id.lower() or
                                lower_search_query in dag.owners.lower()]) |
                           set(webserver_dags_filtered.keys()))

            sorted_dag_ids = sorted(all_dag_ids)
        else:
            webserver_dags_filtered = webserver_dags
            sorted_dag_ids = sorted(set(orm_dags.keys()) | set(webserver_dags.keys()))

        start = current_page * dags_per_page
        end = start + dags_per_page

        num_of_all_dags = len(sorted_dag_ids)
        page_dag_ids = sorted_dag_ids[start:end]
        num_of_pages = int(math.ceil(num_of_all_dags / float(dags_per_page)))

        auto_complete_data = set()
        for dag in webserver_dags_filtered.values():
            auto_complete_data.add(dag.dag_id)
            auto_complete_data.add(dag.owner)
        for dag in orm_dags.values():
            auto_complete_data.add(dag.dag_id)
            auto_complete_data.add(dag.owners)

        return self.render(
            'airflow/dags.html',
            webserver_dags=webserver_dags_filtered,
            orm_dags=orm_dags,
            hide_paused=hide_paused,
            current_page=current_page,
            search_query=arg_search_query if arg_search_query else '',
            page_size=dags_per_page,
            num_of_pages=num_of_pages,
            num_dag_from=start + 1,
            num_dag_to=min(end, num_of_all_dags),
            num_of_all_dags=num_of_all_dags,
            paging=wwwutils.generate_pages(current_page, num_of_pages,
                                           search=arg_search_query,
                                           showPaused=not hide_paused),
            dag_ids_in_page=page_dag_ids,
            auto_complete_data=auto_complete_data,
            view_only=is_view_only(g.user))



######################################################################################
#                                    Widgets
######################################################################################


class AirflowModelListWidget(RenderTemplateWidget):
    template = 'airflow/model_list.html'


######################################################################################
#                                    ModelViews
######################################################################################


class AirflowModelView(ModelView):
    """
    ModelView class for modifiable models
    """
    base_permissions = ['can_add', 'can_list', 'can_edit', 'can_delete']

    list_widget = AirflowModelListWidget

    page_size = PAGE_SIZE


class AirflowModelViewReadOnly(AirflowModelView):
    """
    ModelView class for read-only models
    """
    base_permissions = ['can_list']


class CustomSQLAInterfaceWrapper(SQLAInterface):
    """
    FAB does not know how to handle columns with leading underscores because
    they are not supported by WTForm.

    """
    def __init__(self, obj, session=None):
        super(CustomSQLAInterfaceWrapper, self).__init__(obj, session)
        def clean_column_names():
            if self.list_properties:
                self.list_properties = dict((k.lstrip('_'), v) for k, v in self.list_properties.items())
            if self.list_columns:
                self.list_columns = dict((k.lstrip('_'), v) for k, v in self.list_columns.items())        
        clean_column_names()


# todo: add support for form_widget
class SlaMissModelView(AirflowModelViewReadOnly):
    route_base='/slamiss'

    datamodel = CustomSQLAInterfaceWrapper(models.SlaMiss)

    list_columns = ['dag_id', 'task_id', 'execution_date', 'email_sent', 'timestamp']
    add_columns = ['dag_id', 'task_id', 'execution_date', 'email_sent', 'timestamp']
    edit_columns = ['dag_id', 'task_id', 'execution_date', 'email_sent', 'timestamp']
    search_columns = ['dag_id', 'task_id', 'email_sent', 'timestamp', 'execution_date']
    base_order = ('execution_date', 'desc')
    formatters_columns = {
        'task_id': task_instance_link,
        'execution_date': datetime_f('execution_date'),
        'timestamp': datetime_f('timestamp'),
        'dag_id': dag_link,
    }


class XComModelView(AirflowModelView):
    route_base='/xcom'

    datamodel = CustomSQLAInterfaceWrapper(models.XCom)

    search_columns = ['key', 'value', 'timestamp', 'execution_date', 'task_id', 'dag_id']
    list_columns = ['key', 'value', 'timestamp', 'execution_date', 'task_id', 'dag_id']
    add_columns = ['key', 'value', 'execution_date', 'task_id', 'dag_id']
    edit_columns = ['key', 'value', 'execution_date', 'task_id', 'dag_id']
    base_order = ('execution_date', 'desc')

    @action('muldelete', 'Delete', "Are you sure you want to delete selected records?", single=False)
    def action_muldelete(self, items):
        self.datamodel.delete_all(items)
        self.update_redirect()
        return redirect(self.get_redirect())


class ConnectionModelView(AirflowModelView):
    route_base='/connection'

    datamodel = CustomSQLAInterfaceWrapper(models.Connection)

    extra_fields = ['extra__jdbc__drv_path', 'extra__jdbc__drv_clsname', 'extra__google_cloud_platform__project',
                   'extra__google_cloud_platform__key_path', 'extra__google_cloud_platform__keyfile_dict',
                   'extra__google_cloud_platform__scope']
    list_columns = ['conn_id', 'conn_type', 'host', 'port', 'is_encrypted', 'is_extra_encrypted']
    add_columns = edit_columns = ['conn_id', 'conn_type', 'host', 'schema', 'login', 'password', 'port', 'extra'] + extra_fields
    add_form = edit_form = ConnectionForm
    add_template = 'airflow/conn_create.html'
    edit_template = 'airflow/conn_edit.html'

    base_order = ('conn_id', 'asc')

    @action('muldelete', 'Delete', 'Are you sure you want to delete selected records?', single=False)
    def action_muldelete(self, items):
        self.datamodel.delete_all(items)
        self.update_redirect()
        return redirect(self.get_redirect())

    def process_form(self, form, is_created):
        formdata = form.data
        if formdata['conn_type'] in ['jdbc', 'google_cloud_platform']:
            extra = {
                key: formdata[key]
                for key in self.extra_fields if key in formdata}
            form.extra.data = json.dumps(extra)

    def prefill_form(self, form, pk):
        try:
            d = json.loads(form.data.get('extra', '{}'))
        except Exception:
            d = {}

        for field in self.extra_fields:
            value = d.get(field, '')
            if value:
                field = getattr(form, field)
                field.data = value


class PoolModelView(AirflowModelView):
    route_base='/pool'

    datamodel = CustomSQLAInterfaceWrapper(models.Pool)

    list_columns = ['pool', 'slots', 'used_slots', 'queued_slots']
    add_columns = ['pool', 'slots', 'description']
    edit_columns = ['pool', 'slots', 'description']

    base_order = ('pool', 'asc')

    @action('muldelete', 'Delete', 'Are you sure you want to delete selected records?', single=False)
    def action_muldelete(self, items):
        self.datamodel.delete_all(items)
        self.update_redirect()
        return redirect(self.get_redirect())

    def pool_link(attr):
        pool_id = attr.get('pool')
        if pool_id is not None:
            url = '/taskinstance/list/?_flt_3_pool=' + str(pool_id)
            return Markup("<a href='{url}'>{pool_id}</a>".format(**locals()))
        else:
            return Markup('<span class="label label-danger">Invalid</span>')

    def fused_slots(attr):
        pool_id = attr.get('pool')
        used_slots = attr.get('used_slots')
        if pool_id is not None and used_slots is not None:
            url = '/taskinstance/list/?_flt_3_pool=' + str(pool_id) + '&_flt_3_state=running'
            return Markup("<a href='{url}'>{used_slots}</a>".format(**locals()))
        else:
            return Markup('<span class="label label-danger">Invalid</span>')

    def fqueued_slots(attr):
        pool_id = attr.get('pool')
        queued_slots = attr.get('queued_slots')
        if pool_id is not None and queued_slots is not None:
            url = '/taskinstance/list/?_flt_3_pool=' + str(pool_id) + '&_flt_3_state=queued'
            return Markup("<a href='{url}'>{queued_slots}</a>".format(**locals()))
        else:
            return Markup('<span class="label label-danger">Invalid</span>')

    formatters_columns = {
        'pool': pool_link,
        'used_slots': fused_slots,
        'queued_slots': fqueued_slots
    }

    validators_columns = {
        'pool': [ validators.DataRequired() ],
        'slots': [ validators.NumberRange(min=0) ]
    }


class VariableModelView(AirflowModelView):
    route_base='/variable'

    list_template = 'airflow/variable_list.html'

    datamodel = CustomSQLAInterfaceWrapper(models.Variable)

    list_columns = ['key', 'val', 'is_encrypted']
    add_columns = ['key', 'val']
    edit_columns = ['key', 'val']
    search_columns = ['key', 'val']

    base_order = ('key', 'asc')

    def hidden_field_formatter(attr):
        key = attr.get('key')
        val = attr.get('val')
        if wwwutils.should_hide_value_for_key(key):
            return Markup('*' * 8)
        if val:
            return val
        else:
            return Markup('<span class="label label-danger">Invalid</span>')

    formatters_columns = {
        'val': hidden_field_formatter,
    }

    validators_columns = {
        'key': [ validators.DataRequired() ]
    }

    def prefill_form(self, form, id):
        if wwwutils.should_hide_value_for_key(form.key.data):
            form.val.data = '*' * 8

    @action('muldelete', 'Delete', 'Are you sure you want to delete selected records?', single=False)
    def action_muldelete(self, items):
        self.datamodel.delete_all(items)
        self.update_redirect()
        return redirect(self.get_redirect())

    @action('varexport', 'Export', '', single=False)
    def action_varexport(self, items):
        var_dict = {}
        d = json.JSONDecoder()
        for var in items:
            val = None
            try:
                val = d.decode(var.val)
            except:
                val = var.val
            var_dict[var.key] = val

        response = make_response(json.dumps(var_dict, sort_keys=True, indent=4))
        response.headers["Content-Disposition"] = "attachment; filename=variables.json"
        return response


class JobModelView(AirflowModelViewReadOnly):
    route_base='/job'

    datamodel = CustomSQLAInterfaceWrapper(jobs.BaseJob)

    list_columns = ['id', 'dag_id', 'state', 'job_type', 'start_date', 'end_date', 'latest_heartbeat',
                    'executor_class', 'hostname', 'unixname']
    search_columns = ['id', 'dag_id', 'state', 'job_type', 'start_date', 'end_date', 'latest_heartbeat',
                    'executor_class', 'hostname', 'unixname']

    base_order = ('start_date', 'desc')

    formatters_columns = {
        'start_date': datetime_f('start_date'),
        'end_date': datetime_f('end_date'),
        'hostname': nobr_f('hostname'),
        'state': state_f,
        'latest_heartbeat': datetime_f('latest_heartbeat'),
    }


class DagRunModelView(AirflowModelView):
    route_base='/dagrun'

    datamodel = CustomSQLAInterfaceWrapper(models.DagRun)

    base_permissions = ['can_list']

    list_columns = ['state', 'dag_id', 'execution_date', 'run_id', 'external_trigger']
    search_columns = ['state', 'dag_id', 'execution_date', 'run_id', 'external_trigger']

    base_order = ('execution_date', 'desc')

    add_form = edit_form = DagRunForm

    formatters_columns = {
        'execution_date': datetime_f('execution_date'),
        'state': state_f,
        'start_date': datetime_f('start_date'),
        'dag_id': dag_link,
        'run_id': dag_run_link,
    }

    validators_columns = {
        'dag_id': [ validators.DataRequired() ]
    }

    @action('muldelete', "Delete", "Are you sure you want to delete selected records?", single=False)
    def action_muldelete(self, items):
        self.datamodel.delete_all(items)
        self.update_redirect()
        return redirect(self.get_redirect())
        dirty_ids = []
        for item in items:
            dirty_ids.append(item.dag_id)
        models.DagStat.update(dirty_ids, dirty_only=False, session=session)
        session.close()

    @action('set_running', "Set state to 'running'", '', single=False)
    def action_set_running(self, drs):
        return self.set_dagrun_state(drs, State.RUNNING)

    @action('set_failed', "Set state to 'failed'", '', single=False)
    def action_set_failed(self, drs):
        return self.set_dagrun_state(drs, State.FAILED)

    @action('set_success', "Set state to 'success'", '', single=False)
    def action_set_success(self, drs):
        return self.set_dagrun_state(drs, State.SUCCESS)

    @provide_session
    def set_dagrun_state(self, drs, target_state, session=None):
        try:
            DR = models.DagRun
            count = 0
            dirty_ids = []
            for dr in session.query(DR).filter(
                    DR.id.in_([dagrun.id for dagrun in drs])).all():
                dirty_ids.append(dr.dag_id)
                count += 1
                dr.state = target_state
                if target_state == State.RUNNING:
                    dr.start_date = datetime.now()
                else:
                    dr.end_date = datetime.now()
            session.commit()
            models.DagStat.update(dirty_ids, session=session)
            flash(
                "{count} dag runs were set to '{target_state}'".format(**locals()))
        except Exception as ex:
            raise Exception("Ooops")
            flash('Failed to set state', 'error')
        return redirect(self.route_base + '/list')


class LogModelView(AirflowModelViewReadOnly):
    route_base = '/log'

    datamodel = CustomSQLAInterfaceWrapper(models.Log)

    list_columns = ['id', 'dttm', 'dag_id', 'task_id', 'event', 'execution_date', 'owner', 'extra']
    search_columns = ['dag_id', 'task_id', 'execution_date']

    base_order = ('dttm', 'desc')

    formatters_columns = {
        'dttm': datetime_f('dttm'),
        'execution_date':datetime_f('execution_date'),
        'dag_id':dag_link,
    }


class TaskInstanceModelView(AirflowModelView):
    route_base='/taskinstance'

    datamodel = CustomSQLAInterfaceWrapper(models.TaskInstance)

    base_permissions = ['can_list']

    page_size = PAGE_SIZE

    list_columns = ['state', 'dag_id', 'task_id', 'execution_date', 'operator',
        'start_date', 'end_date', 'duration', 'job_id', 'hostname',
        'unixname', 'priority_weight', 'queue', 'queued_dttm', 'try_number',
        'pool', 'log_url']

    search_columns = ['state', 'dag_id', 'task_id', 'execution_date', 'hostname',
        'queue', 'pool', 'operator', 'start_date', 'end_date']

    base_order = ('job_id', 'asc')

    def log_url_formatter(attr):
        log_url = attr.get('log_url')
        return Markup(
            '<a href="{log_url}">'
            '    <span class="glyphicon glyphicon-book" aria-hidden="true">'
            '</span></a>').format(**locals())

    def duration_f(attr):
        end_date = attr.get('end_date')
        duration = attr.get('duration')
        if end_date and duration:
            return timedelta(seconds=duration)

    formatters_columns = {
        'log_url': log_url_formatter,
        'task_id': task_instance_link,
        'hostname': nobr_f('hostname'),
        'state': state_f,
        'execution_date': datetime_f('execution_date'),
        'start_date': datetime_f('start_date'),
        'end_date': datetime_f('end_date'),
        'queued_dttm': datetime_f('queued_dttm'),
        'dag_id': dag_link,
        'duration': duration_f,
    }

    @provide_session
    @action('clear', lazy_gettext('Clear'), lazy_gettext('Are you sure you want to clear the state of the'
                ' selected task instance(s) and set their dagruns to the running state?'), single=False)
    def action_clear(self, tis, session=None):
        try:
            dag_to_tis = {}

            for ti in tis:
                dag = dagbag.get_dag(ti.dag_id)
                tis = dag_to_tis.setdefault(dag, [])
                tis.append(ti)

            for dag, tis in dag_to_tis.items():
                models.clear_task_instances(tis, session, dag=dag)

            session.commit()
            flash("{0} task instances have been cleared".format(len(tis)))
            self.update_redirect()
            return redirect(self.get_redirect())

        except Exception as ex:
            flash('Failed to clear task instances', 'error')

    @provide_session
    def set_task_instance_state(self, tis, target_state, session=None):
        try:
            count = len(tis)
            for ti in tis:
                ti.set_state(target_state, session)
            session.commit()
            flash(
                "{count} task instances were set to '{target_state}'".format(**locals()))
        except Exception as ex:
            flash('Failed to set state', 'error')

    @action('set_running', "Set state to 'running'", '', single=False)
    def action_set_running(self, tis):
        self.set_task_instance_state(tis, State.RUNNING)
        self.update_redirect()
        return redirect(self.get_redirect())

    @action('set_failed', "Set state to 'failed'", '', single=False)
    def action_set_failed(self, tis):
        self.set_task_instance_state(tis, State.FAILED)
        self.update_redirect()
        return redirect(self.get_redirect())

    @action('set_success', "Set state to 'success'", '', single=False)
    def action_set_success(self, tis):
        self.set_task_instance_state(tis, State.SUCCESS)
        self.update_redirect()
        return redirect(self.get_redirect())

    @action('set_retry', "Set state to 'up_for_retry'", '', single=False)
    def action_set_retry(self, tis):
        self.set_task_instance_state(tis, State.UP_FOR_RETRY)
        self.update_redirect()
        return redirect(self.get_redirect())

    def get_one(self, id):
        """
        As a workaround for AIRFLOW-252, this method overrides Flask-Admin's ModelView.get_one().

        TODO: this method should be removed once the below bug is fixed on Flask-Admin side.
        https://github.com/flask-admin/flask-admin/issues/1226
        """
        task_id, dag_id, execution_date = iterdecode(id)
        execution_date = dateutil.parser.parse(execution_date)
        return self.session.query(self.model).get((task_id, dag_id, execution_date))


class VersionView(AirflowBaseView):
    route_base = '/'

    @expose('version')
    @has_access
    def version(self):
        try:
            airflow_version = airflow.__version__
        except Exception as e:
            airflow_version = None
            logging.error(e)

        # Get the Git repo and git hash
        git_version = None
        try:
            with open(os.path.join(*[settings.AIRFLOW_HOME, 'airflow', 'git_version'])) as f:
                git_version = f.readline()
        except Exception as e:
            logging.error(e)

        # Render information
        title = "Version Info"
        return self.render('airflow/version.html',
                           title=title,
                           airflow_version=airflow_version,
                           git_version=git_version)


class ConfigurationView(AirflowBaseView):
    route_base = '/'

    @expose('configuration')
    @has_access
    def conf(self):
        raw = request.args.get('raw') == "true"
        title = "Airflow Configuration"
        subtitle = conf.AIRFLOW_CONFIG
        with open(conf.AIRFLOW_CONFIG, 'r') as f:
            config = f.read()
        table = [(section, key, value, source)
                 for section, parameters in conf.as_dict(True, True).items()
                 for key, (value, source) in parameters.items()]

        if raw:
            return Response(
                response=config,
                status=200,
                mimetype="application/text")
        else:
            code_html = Markup(highlight(
                config,
                lexers.IniLexer(),  # Lexer call
                HtmlFormatter(noclasses=True))
            )
            return self.render(
                'airflow/config.html',
                pre_subtitle=settings.HEADER + "  v" + airflow.__version__,
                code_html=code_html, title=title, subtitle=subtitle,
                table=table)


class DagModelView(AirflowModelView):
    route_base='/dagmodel'

    page_size = PAGE_SIZE

    datamodel = CustomSQLAInterfaceWrapper(models.DagModel)

    base_permissions = ['can_show', 'can_list']

    list_columns = ['dag_id', 'is_paused', 'last_scheduler_run',
                    'last_expired', 'scheduler_lock', 'fileloc', 'owners']

    formatters_columns = {
        'dag_id': dag_link
    }

    def get_query(self):
        """
        Default filters for model
        """
        return (
            super(DagModelView, self)
                .get_query()
                .filter(or_(models.DagModel.is_active, models.DagModel.is_paused))
                .filter(~models.DagModel.is_subdag)
        )

    def get_count_query(self):
        """
        Default filters for model
        """
        return (
            super(DagModelView, self)
                .get_count_query()
                .filter(models.DagModel.is_active)
                .filter(~models.DagModel.is_subdag)
        )
