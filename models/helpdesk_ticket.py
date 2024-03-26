# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
import requests
import json
# REMEMBER TO INSTALL oauth2client before restarting Odoo by command (pip install oauth2client)
from oauth2client.service_account import ServiceAccountCredentials

import math
from dateutil.relativedelta import relativedelta
from random import randint

from odoo import api, Command, fields, models, tools, _
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT, DEFAULT_SERVER_DATE_FORMAT
from odoo.addons.iap.tools import iap_tools
from odoo.osv import expression
from odoo.exceptions import AccessError, ValidationError
from datetime import datetime
import re
import logging

_logger = logging.getLogger(__name__)

TICKET_PRIORITY = [
    ('0', 'Low priority'),
    ('1', 'Medium priority'),
    ('2', 'High priority'),
    ('3', 'Urgent'),
]


class HelpdeskTag(models.Model):
    _name = 'helpdesk.tag'
    _description = 'Helpdesk Tags'
    _order = 'name'

    def _get_default_color(self):
        return randint(1, 11)

    name = fields.Char(required=True, translate=True)
    color = fields.Integer('Color', default=_get_default_color)

    _sql_constraints = [
        ('name_uniq', 'unique (name)', "Tag name already exists !"),
    ]


class HelpdeskTicketType(models.Model):
    _name = 'helpdesk.ticket.type'
    _description = 'Helpdesk Ticket Type'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)

    _sql_constraints = [
        ('name_uniq', 'unique (name)', "Type name already exists !"),
    ]


class HelpdeskSLAStatus(models.Model):
    _name = 'helpdesk.sla.status'
    _description = "Ticket SLA Status"
    _table = 'helpdesk_sla_status'
    _order = 'deadline ASC, sla_stage_id'
    _rec_name = 'sla_id'

    ticket_id = fields.Many2one('helpdesk.ticket', string='Ticket', required=True, ondelete='cascade', index=True)
    sla_id = fields.Many2one('helpdesk.sla', required=True, ondelete='cascade')
    sla_stage_id = fields.Many2one('helpdesk.stage', related='sla_id.stage_id',
                                   store=True)  # need to be stored for the search in `_sla_reach`
    deadline = fields.Datetime("Deadline", compute='_compute_deadline', compute_sudo=True, store=True)
    reached_datetime = fields.Datetime("Reached Date",
                                       help="Datetime at which the SLA stage was reached for the first time")
    status = fields.Selection([('failed', 'Failed'), ('reached', 'Reached'), ('ongoing', 'Ongoing')], string="Status",
                              compute='_compute_status', compute_sudo=True, search='_search_status')
    color = fields.Integer("Color Index", compute='_compute_color')
    exceeded_days = fields.Float("Excedeed Working Days", compute='_compute_exceeded_days', compute_sudo=True,
                                 store=True,
                                 help="Working days exceeded for reached SLAs compared with deadline. Positive number means the SLA was eached after the deadline.")

    @api.depends('ticket_id.create_date', 'sla_id', 'ticket_id.stage_id')
    def _compute_deadline(self):
        for status in self:
            if (status.deadline and status.reached_datetime) or (
                    status.deadline and not status.sla_id.exclude_stage_ids) or (status.status == 'failed'):
                continue
            deadline = status.ticket_id.create_date
            working_calendar = status.ticket_id.team_id.resource_calendar_id
            if not working_calendar:
                # Normally, having a working_calendar is mandatory
                status.deadline = deadline
                continue

            if status.sla_id.exclude_stage_ids:
                if status.ticket_id.stage_id in status.sla_id.exclude_stage_ids:
                    # We are in the freezed time stage: No deadline
                    status.deadline = False
                    continue

            avg_hour = working_calendar.hours_per_day or 8  # default to 8 working hours/day
            time_days = math.floor(status.sla_id.time / avg_hour)
            if time_days > 0:
                deadline = working_calendar.plan_days(time_days + 1, deadline, compute_leaves=True)
                # We should also depend on ticket creation time, otherwise for 1 day SLA, all tickets
                # created on monday will have their deadline filled with tuesday 8:00
                create_dt = working_calendar.plan_hours(0, status.ticket_id.create_date)
                deadline = deadline.replace(hour=create_dt.hour, minute=create_dt.minute, second=create_dt.second,
                                            microsecond=create_dt.microsecond)

            sla_hours = status.sla_id.time % avg_hour

            if status.sla_id.exclude_stage_ids:
                sla_hours += status._get_freezed_hours(working_calendar)

                # Except if ticket creation time is later than the end time of the working day
                deadline_for_working_cal = working_calendar.plan_hours(0, deadline)
                if deadline_for_working_cal and deadline.day < deadline_for_working_cal.day:
                    deadline = deadline.replace(hour=0, minute=0, second=0, microsecond=0)
            # We should execute the function plan_hours in any case because, in a 1 day SLA environment,
            # if I create a ticket knowing that I'm not working the day after at the same time, ticket
            # deadline will be set at time I don't work (ticket creation time might not be in working calendar).
            status.deadline = working_calendar.plan_hours(sla_hours, deadline, compute_leaves=True)

    @api.depends('deadline', 'reached_datetime')
    def _compute_status(self):
        for status in self:
            if status.reached_datetime and status.deadline:  # if reached_datetime, SLA is finished: either failed or succeeded
                status.status = 'reached' if status.reached_datetime < status.deadline else 'failed'
            else:  # if not finished, deadline should be compared to now()
                status.status = 'ongoing' if not status.deadline or status.deadline > fields.Datetime.now() else 'failed'

    @api.model
    def _search_status(self, operator, value):
        """ Supported operators: '=', 'in' and their negative form. """
        # constants
        datetime_now = fields.Datetime.now()
        positive_domain = {
            'failed': ['|', '&', ('reached_datetime', '=', True), ('deadline', '<=', 'reached_datetime'), '&',
                       ('reached_datetime', '=', False), ('deadline', '<=', fields.Datetime.to_string(datetime_now))],
            'reached': ['&', ('reached_datetime', '=', True), ('reached_datetime', '<', 'deadline')],
            'ongoing': ['|', ('deadline', '=', False), '&', ('reached_datetime', '=', False),
                        ('deadline', '>', fields.Datetime.to_string(datetime_now))]
        }
        # in/not in case: we treat value as a list of selection item
        if not isinstance(value, list):
            value = [value]
        # transform domains
        if operator in expression.NEGATIVE_TERM_OPERATORS:
            # "('status', 'not in', [A, B])" tranformed into "('status', '=', C) OR ('status', '=', D)"
            domains_to_keep = [dom for key, dom in positive_domain if key not in value]
            return expression.OR(domains_to_keep)
        else:
            return expression.OR(positive_domain[value_item] for value_item in value)

    @api.depends('status')
    def _compute_color(self):
        for status in self:
            if status.status == 'failed':
                status.color = 1
            elif status.status == 'reached':
                status.color = 10
            else:
                status.color = 0

    @api.depends('deadline', 'reached_datetime')
    def _compute_exceeded_days(self):
        for status in self:
            if status.reached_datetime and status.deadline and status.ticket_id.team_id.resource_calendar_id:
                if status.reached_datetime <= status.deadline:
                    start_dt = status.reached_datetime
                    end_dt = status.deadline
                    factor = -1
                else:
                    start_dt = status.deadline
                    end_dt = status.reached_datetime
                    factor = 1
                duration_data = status.ticket_id.team_id.resource_calendar_id.get_work_duration_data(start_dt, end_dt,
                                                                                                     compute_leaves=True)
                status.exceeded_days = duration_data['days'] * factor
            else:
                status.exceeded_days = False

    def _get_freezed_hours(self, working_calendar):
        self.ensure_one()
        hours_freezed = 0

        field_stage = self.env['ir.model.fields']._get(self.ticket_id._name, "stage_id")
        freeze_stages = self.sla_id.exclude_stage_ids.ids
        tracking_lines = self.ticket_id.message_ids.tracking_value_ids.filtered(
            lambda tv: tv.field == field_stage).sorted(key="create_date")

        if not tracking_lines:
            return 0

        old_time = self.ticket_id.create_date
        for tracking_line in tracking_lines:
            if tracking_line.old_value_integer in freeze_stages:
                # We must use get_work_hours_count to compute real waiting hours (as the deadline computation is also based on calendar)
                hours_freezed += working_calendar.get_work_hours_count(old_time, tracking_line.create_date)
            old_time = tracking_line.create_date
        if tracking_lines[-1].new_value_integer in freeze_stages:
            # the last tracking line is not yet created
            hours_freezed += working_calendar.get_work_hours_count(old_time, fields.Datetime.now())
        return hours_freezed


class HelpdeskTicket(models.Model):
    _name = 'helpdesk.ticket'
    _description = 'Helpdesk Ticket'
    _order = "create_date desc"
    _inherit = ['portal.mixin', 'mail.thread.cc', 'utm.mixin', 'rating.mixin', 'mail.activity.mixin']

    @api.model
    def default_get(self, fields):
        result = super(HelpdeskTicket, self).default_get(fields)
        if result.get('team_id') and fields:
            team = self.env['helpdesk.team'].browse(result['team_id'])
            if 'user_id' in fields and 'user_id' not in result:  # if no user given, deduce it from the team
                result['user_id'] = team._determine_user_to_assign()[team.id].id
            if 'stage_id' in fields and 'stage_id' not in result:  # if no stage given, deduce it from the team
                result['stage_id'] = team._determine_stage()[team.id].id
        return result

    def _default_team_id(self):
        team_id = self.env['helpdesk.team'].search([('member_ids', 'in', self.env.uid)], limit=1).id
        if not team_id:
            team_id = self.env['helpdesk.team'].search([], limit=1).id
        return team_id

    @api.model
    def _read_group_stage_ids(self, stages, domain, order):
        # write the domain
        # - ('id', 'in', stages.ids): add columns that should be present
        # - OR ('team_ids', '=', team_id) if team_id: add team columns
        search_domain = [('id', 'in', stages.ids)]
        if self.env.context.get('default_team_id'):
            search_domain = ['|', ('team_ids', 'in', self.env.context['default_team_id'])] + search_domain

        return stages.search(search_domain, order=order)

    name = fields.Char(string='Subject', index=True)  # , required=True
    team_id = fields.Many2one('helpdesk.team', string='Helpdesk Team', default=_default_team_id, index=True)
    use_sla = fields.Boolean(related='team_id.use_sla')
    description = fields.Html()
    active = fields.Boolean(default=True)
    ticket_type_id = fields.Many2one('helpdesk.ticket.type', string="Type")
    tag_ids = fields.Many2many('helpdesk.tag', string='Tags')
    company_id = fields.Many2one(related='team_id.company_id', string='Company', store=True, readonly=True)
    color = fields.Integer(string='Color Index')
    kanban_state = fields.Selection([
        ('normal', 'Grey'),
        ('done', 'Green'),
        ('blocked', 'Red')], string='Kanban State',
        copy=False, default='normal', required=True)
    kanban_state_label = fields.Char(compute='_compute_kanban_state_label', string='Column Status', tracking=True)
    legend_blocked = fields.Char(related='stage_id.legend_blocked', string='Kanban Blocked Explanation', readonly=True,
                                 related_sudo=False)
    legend_done = fields.Char(related='stage_id.legend_done', string='Kanban Valid Explanation', readonly=True,
                              related_sudo=False)
    legend_normal = fields.Char(related='stage_id.legend_normal', string='Kanban Ongoing Explanation', readonly=True,
                                related_sudo=False)
    domain_user_ids = fields.Many2many('res.users', compute='_compute_domain_user_ids')
    user_id = fields.Many2one('res.users', string='Assigned to')  # , compute='_compute_user_and_stage_ids',
    # store=True, readonly=False, tracking=True,  domain=lambda self: [('groups_id', 'in', self.env.ref(
    # 'helpdesk.group_helpdesk_user').id)]
    partner_id = fields.Many2one('res.partner', string='Customer')
    partner_ticket_ids = fields.Many2many('helpdesk.ticket', compute='_compute_partner_ticket_count',
                                          string="Partner Tickets")
    partner_ticket_count = fields.Integer('Number of other tickets from the same partner',
                                          compute='_compute_partner_ticket_count')
    # Used to submit tickets from a contact form
    partner_name = fields.Char(string='Customer Name', compute='_compute_partner_name', store=True, readonly=False)
    partner_email = fields.Char(string='Customer Email', compute='_compute_partner_email', store=True, readonly=False)
    partner_phone = fields.Char(string='Customer Phone', compute='_compute_partner_phone', store=True, readonly=False)
    commercial_partner_id = fields.Many2one(related="partner_id.commercial_partner_id")
    closed_by_partner = fields.Boolean('Closed by Partner', readonly=True,
                                       help="If checked, this means the ticket was closed through the customer portal by the customer.")
    cmform = fields.Char(string="CM Form")
    attachment_ids = fields.One2many('ir.attachment', 'res_id',
                                     domain=[('res_model', '=', 'helpdesk.ticket')],
                                     string="Media Attachments")
    close_comment = fields.Text(string="Close Comment")
    unattended = fields.Boolean(string="Unattended", compute="_compute_unattend", store=True,
                                help="In 'Open' state or 'Customer Replied' state taken into consideration name changes")
    # Used in message_get_default_recipients, so if no partner is created, email is sent anyway
    email = fields.Char(related='partner_email', string='Email on Customer', readonly=False)
    priority = fields.Selection(TICKET_PRIORITY, string='Priority', default='0')
    stage_id = fields.Many2one(
        'helpdesk.stage', string='Stage', compute='_compute_user_and_stage_ids', store=True,
        readonly=False, ondelete='restrict', tracking=True, group_expand='_read_group_stage_ids',
        copy=False, index=True, domain="[('team_ids', '=', team_id)]")
    date_last_stage_update = fields.Datetime("Last Stage Update", copy=False, readonly=True)
    # next 4 fields are computed in write (or create)
    assign_date = fields.Datetime("Assign Date")
    assign_hours = fields.Integer("Time to first assignment (hours)", compute='_compute_assign_hours', store=True,
                                  help="This duration is based on the working calendar of the team")
    close_date = fields.Datetime("Close date", copy=False)
    close_hours = fields.Integer("Time to close (hours)", compute='_compute_close_hours', store=True,
                                 help="This duration is based on the working calendar of the team")
    open_hours = fields.Integer("Open Time (hours)", compute='_compute_open_hours', search='_search_open_hours',
                                help="This duration is not based on the working calendar of the team")
    # SLA relative
    sla_ids = fields.Many2many('helpdesk.sla', 'helpdesk_sla_status', 'ticket_id', 'sla_id', string="SLAs", copy=False)
    sla_status_ids = fields.One2many('helpdesk.sla.status', 'ticket_id', string="SLA Status")
    sla_reached_late = fields.Boolean("Has SLA reached late", compute='_compute_sla_reached_late', compute_sudo=True,
                                      store=True)
    sla_deadline = fields.Datetime("SLA Deadline", compute='_compute_sla_deadline', compute_sudo=True, store=True,
                                   help="The closest deadline of all SLA applied on this ticket")
    sla_fail = fields.Boolean("Failed SLA Policy", compute='_compute_sla_fail', search='_search_sla_fail')
    sla_success = fields.Boolean("Success SLA Policy", compute='_compute_sla_success', search='_search_sla_success')

    use_credit_notes = fields.Boolean(related='team_id.use_credit_notes', string='Use Credit Notes')
    use_coupons = fields.Boolean(related='team_id.use_coupons', string='Use Coupons')
    use_product_returns = fields.Boolean(related='team_id.use_product_returns', string='Use Returns')
    use_product_repairs = fields.Boolean(related='team_id.use_product_repairs', string='Use Repairs')
    use_rating = fields.Boolean(related='team_id.use_rating', string='Use Customer Ratings')

    # customer portal: include comment and incoming emails in communication history
    website_message_ids = fields.One2many(
        domain=lambda self: [('model', '=', self._name), ('message_type', 'in', ['email', 'comment'])])
    inventory_serial_number_id = fields.Many2one('stock.production.lot', string='Serial Number')
    project_id = fields.Many2one('project.project', string='Project')
    problem = fields.Many2one('helpdesk.ticket.problem', string="Problem")
    #problem = fields.Many2many('helpdesk.ticket.problem', string="Problem")
    category = fields.Many2one('helpdesk.ticket.categories', required="True", string="Category")
    sub_category_id = fields.Many2one('helpdesk.ticket.subcategory', string="Sub Category")
    open_case = fields.Datetime(string='Open Case Time', required=True)
    close_time = fields.Datetime(string="Close Time")
    prob_solve_time = fields.Datetime(string='Date of Completion')
    closed_by_id = fields.Many2one('res.users', string="Closed By")
    sla_active = fields.Boolean(string="SLA Active")

    # this is for ticket timeline information, technician check-in, check-out for tickets.
    time_to_close = fields.Float(string="Time to close",
                                 help="seconds")  # reeditted from format seconds to : @hafizalwi11jan
    time_to_close_hhmm = fields.Float(string="Time to close: ", help="hh:mm format")
    check_in = fields.Datetime('Technician Check-in', help="Time on which technician check-in for the job")
    check_out = fields.Datetime('Technician Check-out', help="Time on which technician check-out for the job")
    check_in_address = fields.Char(string="Check-in address")
    check_out_address = fields.Char(string="Check-out address")
    check_in_lat = fields.Float(string="Check-in latitude", digits=(12, 15),
                                help="Floating Precision is set to 15 (maximum for longitude and latitude)")
    check_in_long = fields.Float(string="Check-in longitude", digits=(12, 15),
                                 help="Floating Precision is set to 15 (maximum for longitude and latitude)")
    check_out_lat = fields.Float(string="Check-out latitude", digits=(12, 15),
                                 help="Floating Precision is set to 15 (maximum for longitude and latitude)")
    check_out_long = fields.Float(string="Check-out longitude", digits=(12, 15),
                                  help="Floating Precision is set to 15 (maximum for longitude and latitude)")
    address = fields.Char(string='Address')
    department = fields.Char(string='Department')
    equipment_user = fields.Many2one('res.partner', string='Equipment User')
    person_name = fields.Char(string='Reported By')
    is_saved = fields.Boolean(compute='_compute_is_saved')
    ticket_number = fields.Integer(string="Ticket Number", readonly=True, default="5197")
    ticket_number_display = fields.Char(string="Ticket Number Display", compute="_compute_ticket_number_display",
                                        store=True)

    @api.depends('ticket_number')
    def _compute_ticket_number_display(self):
        for record in self:
            record.ticket_number_display = str("TN") + str(record.id) + "-" + datetime.today().strftime(
                '%Y%m%d') + "-" + "{:,}".format(record.id)

    @api.depends('create_date')
    def _compute_is_saved(self):
        for record in self:
            record.is_saved = bool(record.id)

    @api.onchange('inventory_serial_number_id')
    def _onchange_inventory_serial_number_id(self):
        if self.inventory_serial_number_id:
            self.equipment_user = self.inventory_serial_number_id.equipment_user
            self.partner_id = self.inventory_serial_number_id.partner_id
            self.partner_email = self.inventory_serial_number_id.partner_id.email
            self.partner_name = self.inventory_serial_number_id.partner_id.name
            self.partner_phone = self.inventory_serial_number_id.partner_id.phone
            self.project_id = self.inventory_serial_number_id.project_id
            self.department = self.inventory_serial_number_id.equipment_location
            if self.inventory_serial_number_id.partner_id.zip or self.inventory_serial_number_id.partner_id.country_id.name or self.inventory_serial_number_id.partner_id.street or self.inventory_serial_number_id.partner_id.street2 or self.inventory_serial_number_id.partner_id.city or self.inventory_serial_number_id.partner_id.state_id.name:
                if self.inventory_serial_number_id.partner_id.zip:
                    zip = self.inventory_serial_number_id.partner_id.zip + ", "
                else:
                    zip = ""
                if self.inventory_serial_number_id.partner_id.country_id:
                    country = self.inventory_serial_number_id.partner_id.country_id.name + ", "
                else:
                    country = ""
                if self.inventory_serial_number_id.partner_id.street:
                    street = self.inventory_serial_number_id.partner_id.street + ", "
                else:
                    street = ""
                if self.inventory_serial_number_id.partner_id.street2:
                    street2 = self.inventory_serial_number_id.partner_id.street2 + ", "
                else:
                    street2 = ""
                if self.inventory_serial_number_id.partner_id.city:
                    city = self.inventory_serial_number_id.partner_id.city + ", "
                else:
                    city = ""
                if self.inventory_serial_number_id.partner_id.state_id:
                    state = self.inventory_serial_number_id.partner_id.state_id.name
                else:
                    state = ""

                address = "{street}{street2}{city}{zip}{state}".format(
                    zip=zip,
                    country=country,
                    street=street,
                    street2=street2,
                    city=city,
                    state=state)

            else:
                address = ""
            self.address = address

    @api.onchange('equipment_user')
    def _onchange_equipment_user(self):
        if self.equipment_user:
            self.partner_id = self.equipment_user.parent_id
            self.partner_email = self.equipment_user.email
            self.partner_name = self.equipment_user.name
            self.partner_phone = self.equipment_user.phone
            # self.department = self.equipment_user.department
            if self.equipment_user.zip or self.equipment_user.country_id.name or self.equipment_user.street or self.equipment_user.street2 or self.equipment_user.city or self.equipment_user.state_id.name:
                if self.equipment_user.zip:
                    zip = self.equipment_user.zip + ", "
                else:
                    zip = ""
                if self.equipment_user.country_id:
                    country = self.equipment_user.country_id.name + ", "
                else:
                    country = ""
                if self.equipment_user.street:
                    street = self.equipment_user.street + ", "
                else:
                    street = ""
                if self.equipment_user.street2:
                    street2 = self.equipment_user.street2 + ", "
                else:
                    street2 = ""
                if self.equipment_user.city:
                    city = self.equipment_user.city + ", "
                else:
                    city = ""
                if self.equipment_user.state_id:
                    state = self.equipment_user.state_id.name
                else:
                    state = ""

                address = "{street}{street2}{city}{zip}{state}".format(
                    zip=zip,
                    country=country,
                    street=street,
                    street2=street2,
                    city=city,
                    state=state)

            else:
                address = ""
            self.address = address

    @api.onchange('category')
    def _onchange_category(self):
        self.ensure_one()
        for record in self:
            # Get the category record
            category = self.env['helpdesk.ticket.categories'].browse(record.category.id)

            # Assign the category to 'parent_category_id' of the subcategory linked to the ticket
            record.sub_category_id.parent_category_id = category

    @api.constrains('project_id')
    def _check_project(self):
        for ticket in self:
            if ticket.project_id:
                # Perform validation checks here
                pass

    @api.constrains('inventory_serial_number_id')
    def _check_inventory_serial_number(self):
        for ticket in self:
            if ticket.inventory_serial_number_id:
                # Perform validation checks here
                pass

    @api.depends('stage_id', 'kanban_state')
    def _compute_kanban_state_label(self):
        for ticket in self:
            if ticket.kanban_state == 'normal':
                ticket.kanban_state_label = ticket.legend_normal
            elif ticket.kanban_state == 'blocked':
                ticket.kanban_state_label = ticket.legend_blocked
            else:
                ticket.kanban_state_label = ticket.legend_done

    @api.depends('team_id')
    def _compute_domain_user_ids(self):
        helpdesk_user_group_id = self.env.ref('helpdesk.group_helpdesk_user').id
        helpdesk_manager_group_id = self.env.ref('helpdesk.group_helpdesk_manager').id
        users_data = self.env['res.users'].read_group(
            [('groups_id', 'in', [helpdesk_user_group_id, helpdesk_manager_group_id])],
            ['ids:array_agg(id)', 'groups_id'],
            ['groups_id'],
        )
        mapped_data = {data['groups_id'][0]: data['ids'] for data in users_data}
        for ticket in self:
            if ticket.team_id and ticket.team_id.privacy == 'invite' and ticket.team_id.visibility_member_ids:
                manager_ids = mapped_data.get(helpdesk_manager_group_id, [])
                ticket.domain_user_ids = [Command.set(manager_ids + ticket.team_id.visibility_member_ids.ids)]
            else:
                user_ids = mapped_data.get(helpdesk_user_group_id, [])
                ticket.domain_user_ids = [Command.set(user_ids)]

    def _compute_access_url(self):
        super(HelpdeskTicket, self)._compute_access_url()
        for ticket in self:
            ticket.access_url = '/my/ticket/%s' % ticket.id

    @api.depends('sla_status_ids.deadline', 'sla_status_ids.reached_datetime')
    def _compute_sla_reached_late(self):
        """ Required to do it in SQL since we need to compare 2 columns value """
        mapping = {}
        if self.ids:
            self.env.cr.execute("""
                SELECT ticket_id, COUNT(id) AS reached_late_count
                FROM helpdesk_sla_status
                WHERE ticket_id IN %s AND (deadline < reached_datetime OR (deadline < %s AND reached_datetime IS NULL))
                GROUP BY ticket_id
            """, (tuple(self.ids), fields.Datetime.now()))
            mapping = dict(self.env.cr.fetchall())

        for ticket in self:
            ticket.sla_reached_late = mapping.get(ticket.id, 0) > 0

    @api.depends('sla_status_ids.deadline', 'sla_status_ids.reached_datetime')
    def _compute_sla_deadline(self):
        """ Keep the deadline for the last stage (closed one), so a closed ticket can have a status failed.
            Note: a ticket in a closed stage will probably have no deadline
        """
        for ticket in self:
            deadline = False
            status_not_reached = ticket.sla_status_ids.filtered(
                lambda status: not status.reached_datetime and status.deadline)
            ticket.sla_deadline = min(status_not_reached.mapped('deadline')) if status_not_reached else deadline

    @api.depends('sla_deadline', 'sla_reached_late')
    def _compute_sla_fail(self):
        now = fields.Datetime.now()
        for ticket in self:
            if ticket.sla_deadline:
                ticket.sla_fail = (ticket.sla_deadline < now) or ticket.sla_reached_late
            else:
                ticket.sla_fail = ticket.sla_reached_late

    @api.model
    def _search_sla_fail(self, operator, value):
        datetime_now = fields.Datetime.now()
        if (value and operator in expression.NEGATIVE_TERM_OPERATORS) or (
                not value and operator not in expression.NEGATIVE_TERM_OPERATORS):  # is not failed
            return ['&', ('sla_reached_late', '=', False), '|', ('sla_deadline', '=', False),
                    ('sla_deadline', '>=', datetime_now)]
        return ['|', ('sla_reached_late', '=', True), ('sla_deadline', '<', datetime_now)]  # is failed

    @api.depends('sla_deadline', 'sla_reached_late')
    def _compute_sla_success(self):
        now = fields.Datetime.now()
        for ticket in self:
            ticket.sla_success = (ticket.sla_deadline and ticket.sla_deadline > now)

    @api.model
    def _search_sla_success(self, operator, value):
        datetime_now = fields.Datetime.now()
        if (value and operator in expression.NEGATIVE_TERM_OPERATORS) or (
                not value and operator not in expression.NEGATIVE_TERM_OPERATORS):  # is failed
            return [('sla_status_ids.reached_datetime', '>', datetime_now), ('sla_reached_late', '!=', False)]
        return [('sla_status_ids.reached_datetime', '<', datetime_now), ('sla_fail', '=', False)]  # is success

    @api.depends('team_id')
    def _compute_user_and_stage_ids(self):
        for ticket in self.filtered(lambda ticket: ticket.team_id):
            if not ticket.user_id:
                ticket.user_id = ticket.team_id._determine_user_to_assign()[ticket.team_id.id]
            if not ticket.stage_id or ticket.stage_id not in ticket.team_id.stage_ids:
                ticket.stage_id = ticket.team_id._determine_stage()[ticket.team_id.id]

    @api.depends('partner_id')
    def _compute_partner_name(self):
        for ticket in self:
            if ticket.partner_id:
                ticket.partner_name = ticket.partner_id.name

    @api.depends('partner_id')
    def _compute_partner_email(self):
        for ticket in self:
            if ticket.partner_id:
                ticket.partner_email = ticket.partner_id.email

    @api.depends('partner_id')
    def _compute_partner_phone(self):
        for ticket in self:
            if ticket.partner_id:
                ticket.partner_phone = ticket.partner_id.phone

    @api.depends('partner_id', 'partner_email', 'partner_phone')
    def _compute_partner_ticket_count(self):

        def _get_email_to_search(email):
            domain = tools.email_domain_extract(email)
            return ("@" + domain) if domain and domain not in iap_tools._MAIL_DOMAIN_BLACKLIST else email

        for ticket in self:
            domain = []
            partner_ticket = ticket
            if ticket.partner_email:
                email_search = _get_email_to_search(ticket.partner_email)
                domain = expression.OR([domain, [('partner_email', 'ilike', email_search)]])
            if ticket.partner_phone:
                domain = expression.OR([domain, [('partner_phone', 'ilike', ticket.partner_phone)]])
            if ticket.partner_id:
                domain = expression.OR(
                    [domain, [("partner_id", "child_of", ticket.partner_id.commercial_partner_id.id)]])
            if domain:
                partner_ticket = self.search(domain)
            ticket.partner_ticket_ids = partner_ticket
            ticket.partner_ticket_count = len(partner_ticket - ticket._origin) if partner_ticket else 0

    @api.depends('assign_date')
    def _compute_assign_hours(self):
        for ticket in self:
            create_date = fields.Datetime.from_string(ticket.create_date)
            if create_date and ticket.assign_date and ticket.team_id.resource_calendar_id:
                duration_data = ticket.team_id.resource_calendar_id.get_work_duration_data(create_date,
                                                                                           fields.Datetime.from_string(
                                                                                               ticket.assign_date),
                                                                                           compute_leaves=True)
                ticket.assign_hours = duration_data['hours']
            else:
                ticket.assign_hours = False

    @api.depends('create_date', 'close_date')
    def _compute_close_hours(self):
        for ticket in self:
            create_date = fields.Datetime.from_string(ticket.create_date)
            if create_date and ticket.close_date:
                duration_data = ticket.team_id.resource_calendar_id.get_work_duration_data(create_date,
                                                                                           fields.Datetime.from_string(
                                                                                               ticket.close_date),
                                                                                           compute_leaves=True)
                ticket.close_hours = duration_data['hours']
            else:
                ticket.close_hours = False

    @api.depends('close_hours')
    def _compute_open_hours(self):
        for ticket in self:
            if ticket.create_date:  # fix from https://github.com/odoo/enterprise/commit/928fbd1a16e9837190e9c172fa50828fae2a44f7
                if ticket.close_date:
                    time_difference = ticket.close_date - fields.Datetime.from_string(ticket.create_date)
                else:
                    time_difference = fields.Datetime.now() - fields.Datetime.from_string(ticket.create_date)
                ticket.open_hours = (time_difference.seconds) / 3600 + time_difference.days * 24
            else:
                ticket.open_hours = 0

    @api.model
    def _search_open_hours(self, operator, value):
        dt = fields.Datetime.now() - relativedelta(hours=value)

        d1, d2 = False, False
        if operator in ['<', '<=', '>', '>=']:
            d1 = ['&', ('close_date', '=', False), ('create_date', expression.TERM_OPERATORS_NEGATION[operator], dt)]
            d2 = ['&', ('close_date', '!=', False), ('close_hours', operator, value)]
        elif operator in ['=', '!=']:
            subdomain = ['&', ('create_date', '>=', dt.replace(minute=0, second=0, microsecond=0)),
                         ('create_date', '<=', dt.replace(minute=59, second=59, microsecond=99))]
            if operator in expression.NEGATIVE_TERM_OPERATORS:
                subdomain = expression.distribute_not(subdomain)
            d1 = expression.AND([[('close_date', '=', False)], subdomain])
            d2 = ['&', ('close_date', '!=', False), ('close_hours', operator, value)]
        return expression.OR([d1, d2])

    # ------------------------------------------------------------
    # ORM overrides
    # ------------------------------------------------------------

    def name_get(self):
        result = []
        for ticket in self:
            result.append((ticket.id, "%s (#%d)" % (ticket.ticket_number_display, ticket._origin.id)))
        return result

    @api.model
    def create_action(self, action_ref, title, search_view_ref):
        action = self.env["ir.actions.actions"]._for_xml_id(action_ref)
        if title:
            action['display_name'] = title
        if search_view_ref:
            action['search_view_id'] = self.env.ref(search_view_ref).read()[0]
        action['views'] = [(False, view) for view in action['view_mode'].split(",")]

        return {'action': action}

    @api.model_create_multi
    def create(self, list_value):
        now = fields.Datetime.now()
        # determine user_id and stage_id if not given. Done in batch.
        teams = self.env['helpdesk.team'].browse([vals['team_id'] for vals in list_value if vals.get('team_id')])
        team_default_map = dict.fromkeys(teams.ids, dict())
        for team in teams:
            team_default_map[team.id] = {
                'stage_id': team._determine_stage()[team.id].id,
                'user_id': team._determine_user_to_assign()[team.id].id
            }

        # Manually create a partner now since 'generate_recipients' doesn't keep the name. This is
        # to avoid intrusive changes in the 'mail' module
        # TDE TODO: to extract and clean in mail thread
        for vals in list_value:
            # Get next ticket number from the sequence
            vals['ticket_number'] = self.env['ir.sequence'].next_by_code('helpdesk.ticket')
            partner_id = vals.get('partner_id', False)
            partner_name = vals.get('partner_name', False)
            partner_email = vals.get('partner_email', False)
            if partner_name and partner_email and not partner_id:
                parsed_name, parsed_email = self.env['res.partner']._parse_partner_name(partner_email)
                if not parsed_name:
                    parsed_name = partner_name
                if vals.get('team_id'):
                    team = self.env['helpdesk.team'].browse(vals.get('team_id'))
                    company = team.company_id.id
                else:
                    company = False

                vals['partner_id'] = self.env['res.partner'].with_context(default_company_id=company).find_or_create(
                    tools.formataddr((parsed_name, parsed_email))
                ).id

        # determine partner email for ticket with partner but no email given
        partners = self.env['res.partner'].browse([vals['partner_id'] for vals in list_value if
                                                   'partner_id' in vals and vals.get(
                                                       'partner_id') and 'partner_email' not in vals])
        partner_email_map = {partner.id: partner.email for partner in partners}
        partner_name_map = {partner.id: partner.name for partner in partners}

        for vals in list_value:
            if vals.get('team_id'):
                team_default = team_default_map[vals['team_id']]
                if 'stage_id' not in vals:
                    vals['stage_id'] = team_default['stage_id']
                # Note: this will break the randomly distributed user assignment. Indeed, it will be too difficult to
                # equally assigned user when creating ticket in batch, as it requires to search after the last assigned
                # after every ticket creation, which is not very performant. We decided to not cover this user case.
                if 'user_id' not in vals:
                    vals['user_id'] = team_default['user_id']
                if vals.get(
                        'user_id'):  # if a user is finally assigned, force ticket assign_date and reset assign_hours
                    vals['assign_date'] = fields.Datetime.now()
                    vals['assign_hours'] = 0

            # set partner email if in map of not given
            if vals.get('partner_id') in partner_email_map:
                vals['partner_email'] = partner_email_map.get(vals['partner_id'])
            # set partner name if in map of not given
            if vals.get('partner_id') in partner_name_map:
                vals['partner_name'] = partner_name_map.get(vals['partner_id'])

            if vals.get('stage_id'):
                vals['date_last_stage_update'] = now
            ticket = super(HelpdeskTicket, self).create(vals)
            # Send FCM notification
            if ticket.user_id.fcm_token_id:
                # Retrieve all tokens for the user
                fcm_tokens = self.env['fcm.token'].search([('user_id', '=', ticket.user_id.id)])
                for token in fcm_tokens:
                    print('Sending notification to FCM token:', token.token)

                    # Load the service account key JSON file.
                    creds = ServiceAccountCredentials.from_json_keyfile_name(
                        r'C:\Users\DELL\odoo15\addons\helpdesk\sigma-helpdesk-firebase-adminsdk-3ayru-601327b0dd.json',
                        ['https://www.googleapis.com/auth/firebase.messaging']
                    )

                    # Obtain an access token.
                    access_token_info = creds.get_access_token()
                    access_token = access_token_info.access_token

                    print('Access token obtained:', access_token)

                    url = 'https://fcm.googleapis.com/v1/projects/sigma-helpdesk/messages:send'
                    headers = {
                        'Content-Type': 'application/json',
                        'Authorization': 'Bearer ' + access_token,
                    }
                    data = {
                        'message': {
                            'token': token.token,
                            'notification': {
                                'title': 'New Ticket: ' + str(ticket.ticket_number),
                                'body': 'A new ticket has been assigned to you.',
                            },
                        },
                    }
                    try:
                        response = requests.post(url, headers=headers, data=json.dumps(data))
                        if response.status_code == 200:
                            print('Notification sent successfully to:', token.token)
                        else:
                            print('Notification not sent. Response:', response.content)
                    except Exception as e:
                        print('Error sending notification to:', token.token, 'Error:', e)
                print('Retrieved FCM tokens for user:', ticket.user_id.id, 'Tokens:', [t.token for t in fcm_tokens])

        # context: no_log, because subtype already handle this
        tickets = super(HelpdeskTicket, self).create(list_value)

        # make customer follower
        for ticket in tickets:
            if ticket.partner_id:
                ticket.message_subscribe(partner_ids=ticket.partner_id.ids)

            ticket._portal_ensure_token()

        # apply SLA
        tickets.sudo()._sla_apply()

        return tickets

    def write(self, vals):
        # we set the assignation date (assign_date) to now for tickets that are being assigned for the first time
        # same thing for the closing date
        assigned_tickets = closed_tickets = self.browse()
        if vals.get('user_id'):
            assigned_tickets = self.filtered(lambda ticket: not ticket.assign_date)

        if vals.get('stage_id'):
            if self.env['helpdesk.stage'].browse(vals.get('stage_id')).is_close:
                closed_tickets = self.filtered(lambda ticket: not ticket.close_date)
            else:  # auto reset the 'closed_by_partner' flag
                vals['closed_by_partner'] = False
                vals['close_date'] = False

        now = fields.Datetime.now()

        # update last stage date when changing stage
        if 'stage_id' in vals:
            vals['date_last_stage_update'] = now
            if 'kanban_state' not in vals:
                vals['kanban_state'] = 'normal'

        res = super(HelpdeskTicket, self - assigned_tickets - closed_tickets).write(vals)
        res &= super(HelpdeskTicket, assigned_tickets - closed_tickets).write(dict(vals, **{
            'assign_date': now,
        }))
        res &= super(HelpdeskTicket, closed_tickets - assigned_tickets).write(dict(vals, **{
            'close_date': now,
        }))
        res &= super(HelpdeskTicket, assigned_tickets & closed_tickets).write(dict(vals, **{
            'assign_date': now,
            'close_date': now,
        }))

        if vals.get('partner_id'):
            self.message_subscribe([vals['partner_id']])

        # SLA business
        sla_triggers = self._sla_reset_trigger()
        if any(field_name in sla_triggers for field_name in vals.keys()):
            self.sudo()._sla_apply(keep_reached=True)
        if 'stage_id' in vals:
            self.sudo()._sla_reach(vals['stage_id'])

        return res

    # ------------------------------------------------------------
    # Actions and Business methods
    # ------------------------------------------------------------

    @api.model
    def _sla_reset_trigger(self):
        """ Get the list of field for which we have to reset the SLAs (regenerate) """
        return ['team_id', 'priority', 'ticket_type_id', 'tag_ids', 'partner_id']

    def _sla_apply(self, keep_reached=False):
        """ Apply SLA to current tickets: erase the current SLAs, then find and link the new SLAs to each ticket.
            Note: transferring ticket to a team "not using SLA" (but with SLAs defined), SLA status of the ticket will be
            erased but nothing will be recreated.
            :returns recordset of new helpdesk.sla.status applied on current tickets
        """
        # get SLA to apply
        sla_per_tickets = self._sla_find()

        # generate values of new sla status
        sla_status_value_list = []
        for tickets, slas in sla_per_tickets.items():
            sla_status_value_list += tickets._sla_generate_status_values(slas, keep_reached=keep_reached)

        sla_status_to_remove = self.mapped('sla_status_ids')
        if keep_reached:  # keep only the reached one to avoid losing reached_date info
            sla_status_to_remove = sla_status_to_remove.filtered(lambda status: not status.reached_datetime)

        # if we are going to recreate many sla.status, then add norecompute to avoid 2 recomputation (unlink + recreate). Here,
        # `norecompute` will not trigger recomputation. It will be done on the create multi (if value list is not empty).
        if sla_status_value_list:
            sla_status_to_remove.with_context(norecompute=True)

        # unlink status and create the new ones in 2 operations (recomputation optimized)
        sla_status_to_remove.unlink()
        return self.env['helpdesk.sla.status'].create(sla_status_value_list)

    def _sla_find_extra_domain(self):
        self.ensure_one()
        return [
            '|', '|', ('partner_ids', 'parent_of', self.partner_id.ids),
            ('partner_ids', 'child_of', self.partner_id.ids), ('partner_ids', '=', False)
        ]

    def _sla_find(self):
        """ Find the SLA to apply on the current tickets
            :returns a map with the tickets linked to the SLA to apply on them
            :rtype : dict {<helpdesk.ticket>: <helpdesk.sla>}
        """
        tickets_map = {}
        sla_domain_map = {}

        def _generate_key(ticket):
            """ Return a tuple identifying the combinaison of field determining the SLA to apply on the ticket """
            fields_list = self._sla_reset_trigger()
            key = list()
            for field_name in fields_list:
                if ticket._fields[field_name].type == 'many2one':
                    key.append(ticket[field_name].id)
                else:
                    key.append(ticket[field_name])
            return tuple(key)

        for ticket in self:
            if ticket.team_id.use_sla:  # limit to the team using SLA
                key = _generate_key(ticket)
                # group the ticket per key
                tickets_map.setdefault(key, self.env['helpdesk.ticket'])
                tickets_map[key] |= ticket
                # group the SLA to apply, by key
                if key not in sla_domain_map:
                    sla_domain_map[key] = expression.AND([[
                        ('team_id', '=', ticket.team_id.id), ('priority', '<=', ticket.priority),
                        ('stage_id.sequence', '>=', ticket.stage_id.sequence),
                        '|', ('ticket_type_id', '=', ticket.ticket_type_id.id), ('ticket_type_id', '=', False)],
                        ticket._sla_find_extra_domain()])

        result = {}
        for key, tickets in tickets_map.items():  # only one search per ticket group
            domain = sla_domain_map[key]
            slas = self.env['helpdesk.sla'].search(domain)
            result[tickets] = slas.filtered(lambda s: s.tag_ids <= tickets.tag_ids)  # SLA to apply on ticket subset
        return result

    def _sla_generate_status_values(self, slas, keep_reached=False):
        """ Return the list of values for given SLA to be applied on current ticket """
        status_to_keep = dict.fromkeys(self.ids, list())

        # generate the map of status to keep by ticket only if requested
        if keep_reached:
            for ticket in self:
                for status in ticket.sla_status_ids:
                    if status.reached_datetime:
                        status_to_keep[ticket.id].append(status.sla_id.id)

        # create the list of value, and maybe exclude the existing ones
        result = []
        for ticket in self:
            for sla in slas:
                if not (keep_reached and sla.id in status_to_keep[ticket.id]):
                    result.append({
                        'ticket_id': ticket.id,
                        'sla_id': sla.id,
                        'reached_datetime': fields.Datetime.now() if ticket.stage_id == sla.stage_id else False
                        # in case of SLA on first stage
                    })

        return result

    def _sla_reach(self, stage_id):
        """ Flag the SLA status of current ticket for the given stage_id as reached, and even the unreached SLA applied
            on stage having a sequence lower than the given one.
        """
        stage = self.env['helpdesk.stage'].browse(stage_id)
        stages = self.env['helpdesk.stage'].search([('sequence', '<=', stage.sequence), (
            'team_ids', 'in', self.mapped('team_id').ids)])  # take previous stages
        self.env['helpdesk.sla.status'].search([
            ('ticket_id', 'in', self.ids),
            ('sla_stage_id', 'in', stages.ids),
            ('reached_datetime', '=', False),
        ]).write({'reached_datetime': fields.Datetime.now()})

    def assign_ticket_to_self(self):
        self.ensure_one()
        self.user_id = self.env.user

    def action_open_helpdesk_ticket(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("helpdesk.helpdesk_ticket_action_main_tree")
        action.update({
            'domain': [('id', '!=', self.id), ('id', 'in', self.partner_ticket_ids.ids)],
            'context': {'create': False},
        })
        return action

    # ------------------------------------------------------------
    # Messaging API
    # ------------------------------------------------------------

    # DVE FIXME: if partner gets created when sending the message it should be set as partner_id of the ticket.
    def _message_get_suggested_recipients(self):
        recipients = super(HelpdeskTicket, self)._message_get_suggested_recipients()
        try:
            for ticket in self:
                if ticket.partner_id and ticket.partner_id.email:
                    ticket._message_add_suggested_recipient(recipients, partner=ticket.partner_id, reason=_('Customer'))
                elif ticket.partner_email:
                    ticket._message_add_suggested_recipient(recipients, email=ticket.partner_email,
                                                            reason=_('Customer Email'))
        except AccessError:  # no read access rights -> just ignore suggested recipients because this implies modifying followers
            pass
        return recipients

    def _ticket_email_split(self, msg):
        email_list = tools.email_split((msg.get('to') or '') + ',' + (msg.get('cc') or ''))
        # check left-part is not already an alias
        return [
            x for x in email_list
            if x.split('@')[0] not in self.mapped('team_id.alias_name')
        ]

    @api.model
    def message_new(self, msg, custom_values=None):
        values = dict(custom_values or {}, partner_email=msg.get('from'), partner_name=msg.get('from'),
                      partner_id=msg.get('author_id'))
        ticket = super(HelpdeskTicket, self.with_context(mail_notify_author=True)).message_new(msg,
                                                                                               custom_values=values)
        partner_ids = [x.id for x in
                       self.env['mail.thread']._mail_find_partner_from_emails(self._ticket_email_split(msg),
                                                                              records=ticket) if x]
        customer_ids = [p.id for p in self.env['mail.thread']._mail_find_partner_from_emails(
            tools.email_split(values['partner_email']), records=ticket) if p]
        partner_ids += customer_ids
        if customer_ids and not values.get('partner_id'):
            ticket.partner_id = customer_ids[0]
        if partner_ids:
            ticket.message_subscribe(partner_ids)
        return ticket

    def message_update(self, msg, update_vals=None):
        partner_ids = [x.id for x in
                       self.env['mail.thread']._mail_find_partner_from_emails(self._ticket_email_split(msg),
                                                                              records=self) if x]
        if partner_ids:
            self.message_subscribe(partner_ids)
        return super(HelpdeskTicket, self).message_update(msg, update_vals=update_vals)

    def _message_post_after_hook(self, message, msg_vals):
        if self.partner_email and self.partner_id and not self.partner_id.email:
            self.partner_id.email = self.partner_email

        if self.partner_email and not self.partner_id:
            # we consider that posting a message with a specified recipient (not a follower, a specific one)
            # on a document without customer means that it was created through the chatter using
            # suggested recipients. This heuristic allows to avoid ugly hacks in JS.
            new_partner = message.partner_ids.filtered(lambda partner: partner.email == self.partner_email)
            if new_partner:
                self.search([
                    ('partner_id', '=', False),
                    ('partner_email', '=', new_partner.email),
                    ('stage_id.fold', '=', False)]).write({'partner_id': new_partner.id})
        return super(HelpdeskTicket, self)._message_post_after_hook(message, msg_vals)

    def _track_template(self, changes):
        res = super(HelpdeskTicket, self)._track_template(changes)
        ticket = self[0]
        if 'stage_id' in changes and ticket.stage_id.template_id:
            res['stage_id'] = (ticket.stage_id.template_id, {
                'auto_delete_message': True,
                'subtype_id': self.env['ir.model.data']._xmlid_to_res_id('mail.mt_note'),
                'email_layout_xmlid': 'mail.mail_notification_light'
            }
                               )
        return res

    def _creation_subtype(self):
        return self.env.ref('helpdesk.mt_ticket_new')

    def _track_subtype(self, init_values):
        self.ensure_one()
        if 'stage_id' in init_values:
            return self.env.ref('helpdesk.mt_ticket_stage')
        return super(HelpdeskTicket, self)._track_subtype(init_values)

    def _notify_get_groups(self, msg_vals=None):
        """ Handle helpdesk users and managers recipients that can assign
        tickets directly from notification emails. Also give access button
        to portal and portal customers. If they are notified they should
        probably have access to the document. """
        groups = super(HelpdeskTicket, self)._notify_get_groups(msg_vals=msg_vals)

        self.ensure_one()
        for group_name, _group_method, group_data in groups:
            if group_name != 'customer':
                group_data['has_button_access'] = True

        if self.user_id:
            return groups

        local_msg_vals = dict(msg_vals or {})
        take_action = self._notify_get_action_link('assign', **local_msg_vals)
        helpdesk_actions = [{'url': take_action, 'title': _('Assign to me')}]
        helpdesk_user_group_id = self.env.ref('helpdesk.group_helpdesk_user').id
        new_groups = [(
            'group_helpdesk_user',
            lambda pdata: pdata['type'] == 'user' and helpdesk_user_group_id in pdata['groups'],
            {'actions': helpdesk_actions}
        )]
        return new_groups + groups

    def _notify_get_reply_to(self, default=None, records=None, company=None, doc_names=None):
        """ Override to set alias of tickets to their team if any. """
        aliases = self.mapped('team_id').sudo()._notify_get_reply_to(default=default, records=None, company=company,
                                                                     doc_names=None)
        res = {ticket.id: aliases.get(ticket.team_id.id) for ticket in self}
        leftover = self.filtered(lambda rec: not rec.team_id)
        if leftover:
            res.update(
                super(HelpdeskTicket, leftover)._notify_get_reply_to(default=default, records=None, company=company,
                                                                     doc_names=doc_names))
        return res

    # ------------------------------------------------------------
    # Rating Mixin
    # ------------------------------------------------------------

    def rating_apply(self, rate, token=None, feedback=None, subtype_xmlid=None):
        return super(HelpdeskTicket, self).rating_apply(rate, token=token, feedback=feedback,
                                                        subtype_xmlid="helpdesk.mt_ticket_rated")

    def _rating_get_parent_field_name(self):
        return 'team_id'

    # ---------------------------------------------------
    # Mail gateway
    # ---------------------------------------------------

    def _mail_get_message_subtypes(self):
        res = super()._mail_get_message_subtypes()
        if len(self) == 1 and self.team_id:
            team = self.team_id
            optional_subtypes = [('use_credit_notes', self.env.ref('helpdesk.mt_ticket_refund_posted')),
                                 ('use_product_returns', self.env.ref('helpdesk.mt_ticket_return_done')),
                                 ('use_product_repairs', self.env.ref('helpdesk.mt_ticket_repair_done'))]
            for field, subtype in optional_subtypes:
                if not team[field] and subtype in res:
                    res -= subtype
        return res

    def open_close_ticket_wizard(self):
        # Ensure 'self' is a valid record
        if not self:
            _logger.error("Attempted to open close ticket wizard on an invalid record.")
            return {}
        return {
            'name': "Close Support Ticket",
            'type': 'ir.actions.act_window',
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'helpdesk.ticket.close',
            'context': {'default_ticket_id': self.id},
            'target': 'new'
        }
class FcmToken(models.Model):
    _name = 'fcm.token'

    token = fields.Char(string='FCM Token', required=True)
    user_id = fields.Many2one('res.users', string='User', required=True)

    @api.model
    def store_fcm_token(self, user_id, token):
        # Check if a record with the same user ID and token already exists
        existing_token = self.search([('user_id', '=', user_id), ('token', '=', token)], limit=1)
        if not existing_token:
            # If not, create a new record
            self.create({'user_id': user_id, 'token': token})
            print('Stored FCM token for user ', user_id, ': ', token)
        else:
            print('FCM token for user ', user_id, ' already exists: ', token)

class ResUsers(models.Model):
    _inherit = 'res.users'

    fcm_token_id = fields.One2many('fcm.token', 'user_id', string='FCM Token')

class HelpdeskTicketCategories(models.Model):
    _name = "helpdesk.ticket.categories"
    _order = "sequence asc"

    sequence = fields.Integer(string="Sequence")
    name = fields.Char(required=True, translate=True, string='Category Name')
    color = fields.Integer('Color Index', default=1)

    # commented out to solve creating user by admin issues@hafizalwi2Dec
    @api.model
    @api.constrains('name')
    def _check_duplicate(self):
        for record in self:
            category_tags = record.env['helpdesk.ticket.categories'].search_read(
                [('name', 'in', [record.name])])
            if len(category_tags) > 1:
                raise ValidationError(
                    _("You can't create 2 category with the same name!\n Please try creating with a different category name."))

    @api.model
    def create(self, values):
        sequence = self.env['ir.sequence'].next_by_code('helpdesk.ticket.categories')
        values['sequence'] = sequence
        return super(HelpdeskTicketCategories, self).create(values)

    def action_view_ticket(self):
        action = self.env["ir.actions.actions"]._for_xml_id("helpdesk.helpdesk_ticket_action_team")
        action['display_name'] = self.name
        return action

    def action_view_open_ticket_view(self):
        action = self.action_view_ticket()
        action.update({
            'display_name': _("Tickets"),
            'domain': [('team_id', '=', self.id), ('stage_id.is_close', '=', False)],
        })
        return action


class HelpdeskTicketSubCategories(models.Model):
    _name = "helpdesk.ticket.subcategory"
    _order = "sequence asc"

    sequence = fields.Integer(string="Sequence")
    name = fields.Char(required=True, translate=True, string='Sub Category Name')
    parent_category_id = fields.Many2one('helpdesk.ticket.categories', required=True,
                                         string="Category")  # default=lambda self: self.env['helpdesk.ticket'].search(['category.id','=',self.sub_category_id.id]).category.id) #default=lambda self: self.env['helpdesk.ticket'].search(['category.id','=',self.category.id]).category.id)

    @api.model
    @api.constrains('name', 'parent_category_id', )
    def _check_duplicate(self):
        for record in self:
            subcategory_tags = record.env['helpdesk.ticket.subcategory'].search_read(
                [('name', 'in', [record.name]),
                 ('parent_category_id', '=', record.parent_category_id.id)])  # TODO 14/2/2023
            if len(subcategory_tags) > 1:
                raise ValidationError(
                    _("You can't create 2 subcategory with the same name for each category!\n Please try creating with a different subcategory name or a different category name."))

    @api.model
    def create(self, values):
        sequence = self.env['ir.sequence'].next_by_code('helpdesk.ticket.subcategory')
        values['sequence'] = sequence
        return super(HelpdeskTicketSubCategories, self).create(values)


# class WebsiteSupportTicketSubCategoryField(models.Model):
#    _name = "helpdesk.ticket.subcategory.field"
#
#    wsts_id = fields.Many2one('helpdesk.ticket.subcategory', string="Sub Category")
#    name = fields.Char(string="Label", required="True")
#    type = fields.Selection([('textbox', 'Textbox'), ('many2one', 'Dropdown(m2o)')], default="textbox", required="True",
#                            string="Type")
#    model_id = fields.Many2one('ir.model', string="Model")
#    model_name = fields.Char(related="model_id.model", string="Model Name")
#    filter = fields.Char(string="Filter", default="[]", required="True")


class HelpdeskTicketProblem(models.Model):
    _name = "helpdesk.ticket.problem"
    _order = "sequence asc"

    sequence = fields.Integer(string="Sequence")
    name = fields.Char(required=True, string="Problem Name")
    parent_subcategory_id = fields.Many2one('helpdesk.ticket.subcategory', required=True,
                                            string="Sub Category", context="{'default_abc_vendor_id': id}")

    @api.model
    @api.constrains('name', 'parent_subcategory_id', )
    def _check_duplicate(self):
        for record in self:
            problem_tags = self.env['helpdesk.ticket.problem'].search_read(
                [('name', 'in', [record.name]), ('parent_subcategory_id', '=', record.parent_subcategory_id.id)])
            if len(problem_tags) > 1:
                raise ValidationError(
                    _("You can't create 2 problem tags with the same problem_name and subcategory!\n Please try creating with a different subcategory or a different problem name."))

    @api.model
    def create(self, values):
        sequence = self.env['ir.sequence'].next_by_code('helpdesk.ticket.problem')
        values['sequence'] = sequence
        return super(HelpdeskTicketProblem, self).create(values)


class WebsiteSupportTicketClose(models.TransientModel):
    _name = "helpdesk.ticket.close"

    ticket_id = fields.Many2one('helpdesk.ticket', string="Ticket ID")
    message = fields.Text(string="Close Message")
    cm_form = fields.Char(string="CM Form")
    case_done = fields.Datetime(string="Date of completion")
    template_id = fields.Many2one('mail.template', string="Mail Template",
                                  domain="[('model_id','=','helpdesk.ticket'), ('built_in','=',False)]")
    support_level_id = fields.Many2one('helpdesk.ticket.level', string="Support Level")  # hafiz 18 feb 2022
    attachment_ids = fields.Many2many('ir.attachment', 'sms_close_attachment_rel', 'sms_close_id', 'attachment_id',
                                      'Attachments')

    @api.onchange('template_id')
    def _onchange_template_id(self):
        if self.template_id:
            try:
                values = \
                    self.env['mail.compose.message'].generate_email_for_composer(self.template_id.id,
                                                                                 [self.ticket_id.id])[
                        self.ticket_id.id]
                self.message = values['body']
            except Exception as e:
                _logger.error("Error generating email for composer: %s", e)

    def close_ticket(self):  # @hafiz15dec
        # Ensure 'self.ticket_id' is a valid record
        if not self.ticket_id:
            _logger.error("Attempted to close a ticket without a valid ticket_id.")
            return

        self.ticket_id.close_time = datetime.now()

        # Also set the date for gamification
        self.ticket_id.close_date = datetime.today().date()

        # Directly subtract datetime objects to get the difference
        diff_time = self.ticket_id.close_time - self.ticket_id.create_date

        diff_time_days = diff_time.days
        diff_time_seconds = diff_time.seconds
        diff_time_hours = diff_time_seconds / 3600 + (diff_time_days * 24)

        self.ticket_id.time_to_close_hhmm = float(str(diff_time_hours))
        self.ticket_id.time_to_close = diff_time.total_seconds()

        # Assign the ticket to the "Solved" stage
        self.ticket_id.stage_id = self.env.ref('helpdesk.stage_solved')

        # Save the attachment_ids if any
        if self.attachment_ids:
            self.ticket_id.attachment_ids = [(6, 0, self.attachment_ids.ids)]

        # result = self.env['ir.model.data'].sudo()._xmlid_lookup('helpdesk.website_ticket_state_staff_closed')
        # print(result)
        # xml_id = 'helpdesk.website_ticket_state_staff_closed'
        # data_id = self.env['ir.model.data'].sudo().search([('name', '=', xml_id)], limit=1)
        # if data_id:
        #     print('XML ID exists and is associated with the record:', data_id.res_id)
        # else:
        #     print('XML ID does not exist or is not associated with any record')

        # # # Assuming 'closed_state' is a tuple returned by _xmlid_lookup
        # # # Correctly unpack the tuple to get the record ID and model name
        # # record_id, model_name, additional_info = self.env['ir.model.data'].sudo()._xmlid_lookup(
        # #     'helpdesk.website_ticket_state_staff_closed')
        #  #
        # # # Now, use the model name and record ID to browse the record
        # # closed_state = self.env[model_name].browse(record_id)
        #
        # # Assuming 'helpdesk.website_ticket_state_staff_closed' is a reference to a record in the 'helpdesk.stage' model
        # xml_id = 'helpdesk.website_ticket_state_staff_closed'
        # record_id, record_model, record_noupdate = self.env['ir.model.data'].sudo()._xmlid_lookup(xml_id)
        #
        # self.env['helpdesk.stage'].invalidate_cache(['name'], [record_id])
        # # Now, use the record ID to search the record
        # # closed_state = self.env['helpdesk.stage'].search([('id', '=', record_id)], limit=1)
        #
        # # Now, use the record ID to browse the record
        # closed_state = self.env['helpdesk.stage'].browse(record_id)
        # print('closed_state: ',closed_state)
        # print('record_id: ', record_id)
        # # Flush the operations
        # self.env['helpdesk.stage'].flush()
        #
        # closed_state.refresh()
        # print('record_id after refresh: ', record_id)
        # print('closed_stage_name: ', closed_state.name)
        # # We record state change manually since it would spam the chatter if every 'Staff Replied' and 'Customer Replied' gets recorded
        # closed_stage_name = "Unknown Stage"
        # if closed_state:
        #     ticket_stage_name = self.ticket_id.stage_id.read(['name'])[0]['name']
        #     print('ticket_stage_name: ', ticket_stage_name)
        #     if closed_state.name:
        #         closed_stage_name = closed_state.name
        #         print('closed_state: ', closed_state, 'closed_stage_name: ', closed_stage_name)
        #     else:
        #         print('closed_state Record does not exist')
        #     message = "<ul class=\"o_mail_thread_message_tracking\">\n<li>State:<span> " + ticket_stage_name + " </span><b>-></b> " + closed_stage_name + " </span></li></ul>"
        #     self.ticket_id.message_post(body=message, subject="Ticket Closed by Staff")
        # else:
        #     _logger.error("Failed to retrieve closed state record.")

        # values = {}
        # xml_id = 'solved_ticket_request_email_template'
        # record_id, record_model, record_noupdate = self.env['ir.model.data'].sudo()._xmlid_lookup(xml_id)
        #
        # # Now, use the record ID to browse the record
        # if record_model == 'mail.template':
        #     email_wrapper = self.env['mail.template'].browse(record_id)
        #     fields = ['subject', 'body_html', 'email_from', 'email_to', 'partner_to', 'email_cc', 'reply_to']
        #     values = email_wrapper.generate_email([self.id], fields)[self.id]
        #     values['model'] = "helpdesk.ticket"
        #     values['res_id'] = self.ticket_id.id
        # else:
        #     print('Record not found or incorrect model')
        #
        # for attachment in self.attachment_ids:
        #     values['attachment_ids'].append((4, attachment.id))
        #
        # send_mail = self.env['mail.mail'].create(values)
        # send_mail.send()

        cleanbreak = re.compile('<br\s*?>')
        cleanedbreak = re.sub(cleanbreak, '\n', self.message)
        cleanr = re.compile('<.*?>|&([a-z0-9]+|#[0-9]{1,6}|#x[0-9a-f]{1,6});')

        self.ticket_id.close_comment = re.sub(cleanr, '', cleanedbreak)
        self.ticket_id.cmform = self.cm_form
        self.ticket_id.prob_solve_time = self.case_done
        self.ticket_id.closed_by_id = self.env.user.id
        # self.ticket_id.stage = closed_state.id

        self.ticket_id.sla_active = False

        # # Auto send out survey
        # setting_auto_send_survey = self.env['ir.default'].get('helpdesk.settings', 'auto_send_survey')
        # if setting_auto_send_survey:
        #     self.ticket_id.send_survey()


class WebsiteSupportTicketMerge(models.TransientModel):
    _name = "helpdesk.ticket.merge"

    ticket_id = fields.Many2one('helpdesk.ticket', ondelete="cascade", string="Support Ticket")
    merge_ticket_id = fields.Many2one('helpdesk.ticket', ondelete="cascade", required="True",
                                      string="Merge With")

    def merge_tickets(self):

        self.ticket_id.close_time = datetime.datetime.now()

        # Also set the date for gamification
        self.ticket_id.close_date = datetime.date.today()

        diff_time = datetime.datetime.strptime(self.ticket_id.close_time,
                                               DEFAULT_SERVER_DATETIME_FORMAT) - datetime.datetime.strptime(
            self.ticket_id.create_date, DEFAULT_SERVER_DATETIME_FORMAT)

        self.ticket_id.time_to_close = diff_time.seconds

        closed_state = self.env['ir.model.data'].sudo().get_object('website_supportzayd',
                                                                   'website_ticket_state_staff_closed')
        self.ticket_id.state = closed_state.id

        # Lock the ticket to prevent reopening
        self.ticket_id.ticket = True

        # Send merge email
        setting_ticket_merge_email_template_id = self.env['ir.default'].get('helpdesk.settings',
                                                                            'ticket_merge_email_template_id')
        if setting_ticket_merge_email_template_id:
            mail_template = self.env['mail.template'].browse(setting_ticket_merge_email_template_id)
        else:
            # BACK COMPATABLITY FAIL SAFE
            mail_template = self.env['ir.model'].get_object('website_supportzayd', 'support_ticket_merge')

        mail_template.send_mail(self.id, True)

        # Add as follower to new ticket
        if self.ticket_id.partner_id:
            self.merge_ticket_id.message_subscribe([self.ticket_id.partner_id.id])

        return {
            'name': "Support Ticket",
            'type': 'ir.actions.act_window',
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'helpdesk.ticket',
            'res_id': self.merge_ticket_id.id
        }


class WebsiteSupportTicketCompose(models.Model):
    _name = "helpdesk.ticket.compose"

    ticket_id = fields.Many2one('helpdesk.ticket', string='Ticket ID')
    partner_id = fields.Many2one('res.partner', string="Partner", readonly="True")
    email = fields.Char(string="Email", readonly="True")
    email_cc = fields.Char(string="Cc")
    subject = fields.Char(string="Subject", readonly="True")
    body = fields.Text(string="Message Body")
    template_id = fields.Many2one('mail.template', string="Mail Template",
                                  domain="[('model_id','=','helpdesk.ticket'), ('built_in','=',False)]")
    approval = fields.Boolean(string="Approval")
    planned_time = fields.Datetime(string="Planned Time")
    attachment_ids = fields.Many2many('ir.attachment', 'sms_compose_attachment_rel', 'sms_compose_id', 'attachment_id',
                                      'Attachments')

    @api.onchange('template_id')
    def _onchange_template_id(self):
        if self.template_id:
            values = \
                self.env['mail.compose.message'].generate_email_for_composer(self.template_id.id, [self.ticket_id.id])[
                    self.ticket_id.id]
            self.body = values['body']

    def send_reply(self):

        # Change the approval state before we send the mail
        if self.approval:
            # Change the ticket state to awaiting approval
            awaiting_approval_state = self.env['ir.model.data'].get_object('website_supportzayd',
                                                                           'website_ticket_state_awaiting_approval')
            self.ticket_id.state = awaiting_approval_state.id

            # One support request per ticket...
            self.ticket_id.planned_time = self.planned_time
            self.ticket_id.approval_message = self.body
            self.ticket_id.sla_active = False

        # Send email
        values = {}

        setting_staff_reply_email_template_id = self.env['ir.default'].get('helpdesk.settings',
                                                                           'staff_reply_email_template_id')

        if setting_staff_reply_email_template_id:
            email_wrapper = self.env['mail.template'].browse(setting_staff_reply_email_template_id)

        values = email_wrapper.generate_email([self.id])[self.id]
        values['model'] = "helpdesk.ticket"
        values['res_id'] = self.ticket_id.id
        values['reply_to'] = email_wrapper.reply_to

        if self.email_cc:
            values['email_cc'] = self.email_cc

        for attachment in self.attachment_ids:
            values['attachment_ids'].append((4, attachment.id))

        send_mail = self.env['mail.mail'].create(values)
        send_mail.send()

        # Add to the message history to keep the data clean from the rest HTML
        self.env['helpdesk.ticket.message'].create({'ticket_id': self.ticket_id.id, 'by': 'staff',
                                                    'content': self.body.replace("<p>", "").replace("</p>", "")})

        # Post in message history
        # self.ticket_id.message_post(body=self.body, subject=self.subject, message_type='comment', subtype='mt_comment')

        if self.approval:
            # Also change the approval
            awaiting_approval = self.env['ir.model.data'].get_object('website_supportzayd', 'awaiting_approval')
            self.ticket_id.approval_id = awaiting_approval.id
        else:
            # Change the ticket state to staff replied
            staff_replied = self.env['ir.model.data'].get_object('website_supportzayd',
                                                                 'website_ticket_state_staff_replied')
            self.ticket_id.state = staff_replied.id


class WebsiteSupportTicketLevel(models.Model):
    _name = "helpdesk.ticket.level"

    name = fields.Char(string="Support Level")  # this is to store model char ?? @hafizalwi1dec2021
