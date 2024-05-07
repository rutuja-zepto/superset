# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import functools
import logging
from datetime import datetime
from typing import Any, Callable, cast

import simplejson as json
import yaml
from flask import (
    abort,
    current_app as app,
    flash,
    g,
    redirect,
    Response,
)
from flask_appbuilder import BaseView, Model, ModelView
from flask_appbuilder.actions import action
from flask_appbuilder.forms import DynamicForm
from flask_appbuilder.models.sqla.filters import BaseFilter
from flask_appbuilder.widgets import ListWidget
from flask_babel import gettext as __, lazy_gettext as _
from flask_jwt_extended.exceptions import NoAuthorizationError
from flask_wtf.form import FlaskForm
from sqlalchemy import exc
from sqlalchemy.orm import Query
from werkzeug.exceptions import HTTPException
from wtforms import Form
from wtforms.fields.core import Field, UnboundField

from superset.connectors.sqla import models
from superset.exceptions import (
    SupersetErrorException,
    SupersetErrorsException,
    SupersetException,
    SupersetSecurityException,
)
from superset.extensions import security_manager
from superset.initialization import common_bootstrap_payload
from superset.models.helpers import ImportExportMixin
from superset.superset_typing import FlaskResponse
from superset.utils import core as utils
from superset.utils.filters import get_dataset_access_filters
from superset.views.utils import get_error_msg

from .utils import bootstrap_user_data, json_errors_response, json_success

logger = logging.getLogger(__name__)


def json_error_response(
    msg: str | None = None,
    status: int = 500,
    payload: dict[str, Any] | None = None,
) -> FlaskResponse:
    payload = payload or {"error": f"{msg}"}

    return Response(
        json.dumps(payload, default=utils.json_iso_dttm_ser, ignore_nan=True),
        status=status,
        mimetype="application/json",
    )


def data_payload_response(payload_json: str, has_error: bool = False) -> FlaskResponse:
    status = 400 if has_error else 200
    return json_success(payload_json, status=status)


def generate_download_headers(
    extension: str, filename: str | None = None
) -> dict[str, Any]:
    filename = filename if filename else datetime.now().strftime("%Y%m%d_%H%M%S")
    content_disp = f"attachment; filename={filename}.{extension}"
    headers = {"Content-Disposition": content_disp}
    return headers


def deprecated(
    eol_version: str = "5.0.0",
    new_target: str | None = None,
) -> Callable[[Callable[..., FlaskResponse]], Callable[..., FlaskResponse]]:
    """
    A decorator to set an API endpoint from SupersetView has deprecated.
    Issues a log warning
    """

    def _deprecated(f: Callable[..., FlaskResponse]) -> Callable[..., FlaskResponse]:
        def wraps(self: BaseSupersetView, *args: Any, **kwargs: Any) -> FlaskResponse:
            message = (
                "%s.%s "
                "This API endpoint is deprecated and will be removed in version %s"
            )
            logger_args = [
                self.__class__.__name__,
                f.__name__,
                eol_version,
            ]
            if new_target:
                message += " . Use the following API endpoint instead: %s"
                logger_args.append(new_target)
            logger.warning(message, *logger_args)
            return f(self, *args, **kwargs)

        return functools.update_wrapper(wraps, f)

    return _deprecated


def api(f: Callable[..., FlaskResponse]) -> Callable[..., FlaskResponse]:
    """
    A decorator to label an endpoint as an API. Catches uncaught exceptions and
    return the response in the JSON format
    """

    def wraps(self: BaseSupersetView, *args: Any, **kwargs: Any) -> FlaskResponse:
        try:
            return f(self, *args, **kwargs)
        except NoAuthorizationError:
            logger.warning("Api failed- no authorization", exc_info=True)
            return json_error_response(get_error_msg(), status=401)
        except Exception as ex:  # pylint: disable=broad-except
            logger.exception(ex)
            return json_error_response(get_error_msg())

    return functools.update_wrapper(wraps, f)


def handle_api_exception(
    f: Callable[..., FlaskResponse],
) -> Callable[..., FlaskResponse]:
    """
    A decorator to catch superset exceptions. Use it after the @api decorator above
    so superset exception handler is triggered before the handler for generic
    exceptions.
    """

    def wraps(self: BaseSupersetView, *args: Any, **kwargs: Any) -> FlaskResponse:
        try:
            return f(self, *args, **kwargs)
        except SupersetSecurityException as ex:
            logger.warning("SupersetSecurityException", exc_info=True)
            return json_errors_response(
                errors=[ex.error], status=ex.status, payload=ex.payload
            )
        except SupersetErrorsException as ex:
            logger.warning(ex, exc_info=True)
            return json_errors_response(errors=ex.errors, status=ex.status)
        except SupersetErrorException as ex:
            logger.warning("SupersetErrorException", exc_info=True)
            return json_errors_response(errors=[ex.error], status=ex.status)
        except SupersetException as ex:
            if ex.status >= 500:
                logger.exception(ex)
            return json_error_response(
                utils.error_msg_from_exception(ex), status=ex.status
            )
        except HTTPException as ex:
            logger.exception(ex)
            return json_error_response(
                utils.error_msg_from_exception(ex), status=cast(int, ex.code)
            )
        except (exc.IntegrityError, exc.DatabaseError, exc.DataError) as ex:
            logger.exception(ex)
            return json_error_response(utils.error_msg_from_exception(ex), status=422)
        except Exception as ex:  # pylint: disable=broad-except
            logger.exception(ex)
            return json_error_response(utils.error_msg_from_exception(ex))

    return functools.update_wrapper(wraps, f)


class BaseSupersetView(BaseView):
    @staticmethod
    def json_response(obj: Any, status: int = 200) -> FlaskResponse:
        return Response(
            json.dumps(obj, default=utils.json_int_dttm_ser, ignore_nan=True),
            status=status,
            mimetype="application/json",
        )

    def render_app_template(
        self, extra_bootstrap_data: dict[str, Any] | None = None
    ) -> FlaskResponse:
        payload = {
            "user": bootstrap_user_data(g.user, include_perms=True),
            "common": common_bootstrap_payload(),
            **(extra_bootstrap_data or {}),
        }
        return self.render_template(
            "superset/spa.html",
            entry="spa",
            bootstrap_data=json.dumps(
                payload, default=utils.pessimistic_json_iso_dttm_ser
            ),
        )


class SupersetListWidget(ListWidget):  # pylint: disable=too-few-public-methods
    template = "superset/fab_overrides/list.html"


class SupersetModelView(ModelView):
    page_size = 100
    list_widget = SupersetListWidget

    def render_app_template(self) -> FlaskResponse:
        payload = {
            "user": bootstrap_user_data(g.user, include_perms=True),
            "common": common_bootstrap_payload(),
        }
        return self.render_template(
            "superset/spa.html",
            entry="spa",
            bootstrap_data=json.dumps(
                payload, default=utils.pessimistic_json_iso_dttm_ser
            ),
        )


class ListWidgetWithCheckboxes(ListWidget):  # pylint: disable=too-few-public-methods
    """An alternative to list view that renders Boolean fields as checkboxes

    Works in conjunction with the `checkbox` view."""

    template = "superset/fab_overrides/list_with_checkboxes.html"


def validate_json(form: Form, field: Field) -> None:  # pylint: disable=unused-argument
    try:
        json.loads(field.data)
    except Exception as ex:
        logger.exception(ex)
        raise Exception(  # pylint: disable=broad-exception-raised
            _("json isn't valid")
        ) from ex


class YamlExportMixin:  # pylint: disable=too-few-public-methods
    """
    Override this if you want a dict response instead, with a certain key.
    Used on DatabaseView for cli compatibility
    """

    yaml_dict_key: str | None = None

    @action("yaml_export", __("Export to YAML"), __("Export to YAML?"), "fa-download")
    def yaml_export(
        self, items: ImportExportMixin | list[ImportExportMixin]
    ) -> FlaskResponse:
        if not isinstance(items, list):
            items = [items]

        data = [t.export_to_dict() for t in items]

        return Response(
            yaml.safe_dump({self.yaml_dict_key: data} if self.yaml_dict_key else data),
            headers=generate_download_headers("yaml"),
            mimetype="application/text",
        )


class DeleteMixin:  # pylint: disable=too-few-public-methods
    def _delete(self: BaseView, primary_key: int) -> None:
        """
        Delete function logic, override to implement different logic
        deletes the record with primary_key = primary_key

        :param primary_key:
            record primary key to delete
        """
        item = self.datamodel.get(primary_key, self._base_filters)
        if not item:
            abort(404)
        try:
            self.pre_delete(item)
        except Exception as ex:  # pylint: disable=broad-except
            flash(str(ex), "danger")
        else:
            view_menu = security_manager.find_view_menu(item.get_perm())
            pvs = (
                security_manager.get_session.query(
                    security_manager.permissionview_model
                )
                .filter_by(view_menu=view_menu)
                .all()
            )

            if self.datamodel.delete(item):
                self.post_delete(item)

                for pv in pvs:
                    security_manager.get_session.delete(pv)

                if view_menu:
                    security_manager.get_session.delete(view_menu)

                security_manager.get_session.commit()

            flash(*self.datamodel.message)
            self.update_redirect()

    @action(
        "muldelete", __("Delete"), __("Delete all Really?"), "fa-trash", single=False
    )
    def muldelete(self: BaseView, items: list[Model]) -> FlaskResponse:
        if not items:
            abort(404)
        for item in items:
            try:
                self.pre_delete(item)
            except Exception as ex:  # pylint: disable=broad-except
                flash(str(ex), "danger")
            else:
                self._delete(item.id)
        self.update_redirect()
        return redirect(self.get_redirect())


class DatasourceFilter(BaseFilter):  # pylint: disable=too-few-public-methods
    def apply(self, query: Query, value: Any) -> Query:
        if security_manager.can_access_all_datasources():
            return query
        query = query.join(
            models.Database,
            models.Database.id == self.model.database_id,
        )
        return query.filter(get_dataset_access_filters(self.model))


class CsvResponse(Response):
    """
    Override Response to take into account csv encoding from config.py
    """

    default_mimetype: str = "text/csv"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.charset: str = app.config["CSV_EXPORT"].get("encoding", "utf-8")
        super().__init__(*args, **kwargs)


class XlsxResponse(Response):
    """
    Override Response to use xlsx mimetype
    """

    charset = "utf-8"
    default_mimetype = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def bind_field(
    _: Any, form: DynamicForm, unbound_field: UnboundField, options: dict[Any, Any]
) -> Field:
    """
    Customize how fields are bound by stripping all whitespace.

    :param form: The form
    :param unbound_field: The unbound field
    :param options: The field options
    :returns: The bound field
    """

    filters = unbound_field.kwargs.get("filters", [])
    filters.append(lambda x: x.strip() if isinstance(x, str) else x)
    return unbound_field.bind(form=form, filters=filters, **options)


FlaskForm.Meta.bind_field = bind_field
