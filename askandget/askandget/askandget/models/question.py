import logging
import datetime
from django.conf import settings
from django.utils.datastructures import SortedDict
from django.db import models
from django.contrib.auth.models import User
from django.utils.http import urlquote as django_urlquote
from django.core.urlresolvers import reverse
from django.core import exceptions as django_exceptions
from django.contrib.sitemaps import ping_google
from django.utils.translation import ugettext as _
import askbot
import askbot.conf
from askbot import exceptions
from askbot.models.tag import Tag
from askbot.models.base import AnonymousContent, DeletableContent, ContentRevision
from askbot.models.base import parse_post_text, parse_and_save_post
from askbot.models import content
from askbot.models import signals
from askbot import const
from askbot.utils.lists import LazyList
from askbot.utils.slug import slugify
from askbot.utils import markup
from askbot.utils.html import sanitize_html

#todo: too bad keys are duplicated see const sort methods
QUESTION_ORDER_BY_MAP = {
    'age-desc': '-added_at',
    'age-asc': 'added_at',
    'activity-desc': '-last_activity_at',
    'activity-asc': 'last_activity_at',
    'answers-desc': '-answer_count',
    'answers-asc': 'answer_count',
    'votes-desc': '-score',
    'votes-asc': 'score',
    'relevance-desc': None#this is a special case for postges only
}

class QuestionManager(models.Manager):
    def create_new(self, title=None,author=None,added_at=None, wiki=False,tagnames=None, text=None):

        question = Question(
            title = title,
            author = author,
            added_at = added_at,
            last_activity_at = added_at,
            last_activity_by = author,
            wiki = wiki,
            tagnames = tagnames,
            #html field is denormalized in .save() call
            text = text,
            #summary field is denormalized in .save() call
        )
        if question.wiki:
            #DATED COMMENT
            #todo: this is confusing - last_edited_at field
            #is used as an indicator whether question has been edited
            #but in principle, post creation should count as edit as well
            question.last_edited_by = question.author
            question.last_edited_at = added_at
            question.wikified_at = added_at

        question.parse_and_save(author = author)
        question.update_tags(tagnames = tagnames, user = author, timestamp = added_at)

        question.add_revision(
            author=author,
            text=text,
            comment=const.POST_STATUS['default_version'],
            revised_at=added_at,
        )
        return question

    def run_advanced_search(
                        self,
                        request_user = None,
                        search_state = None
                    ):
        """all parameters are guaranteed to be clean
        however may not relate to database - in that case
        a relvant filter will be silently dropped
        """

        scope_selector = getattr(
                            search_state,
                            'scope',
                            const.DEFAULT_POST_SCOPE
                        )

        search_query = search_state.query
        tag_selector = search_state.tags
        author_selector = search_state.author

        sort_method = getattr(
                            search_state, 
                            'sort',
                            const.DEFAULT_POST_SORT_METHOD
                        )

        qs = self.filter(deleted=False)#todo - add a possibility to see deleted questions

        #return metadata
        meta_data = {}
        if tag_selector: 
            for tag in tag_selector:
                qs = qs.filter(tags__name = tag)

        if search_query:
            if settings.DATABASE_ENGINE == 'mysql':
                qs = qs.filter( 
                            models.Q(title__search = search_query) \
                           | models.Q(text__search = search_query) \
                           | models.Q(tagnames__search = search_query) \
                           | models.Q(answers__text__search = search_query)
                        )
            elif settings.DATABASE_ENGINE == 'postgresql_psycopg2':
                rank_clause = "ts_rank(question.text_search_vector, to_tsquery(%s))";
                search_query = '&'.join(search_query.split())
                extra_params = ("'" + search_query + "'",)
                extra_kwargs = {
                    'select': {'relevance': rank_clause},
                    'where': ['text_search_vector @@ to_tsquery(%s)'],
                    'params': extra_params,
                    'select_params': extra_params,
                }
                if askbot.conf.should_show_sort_by_relevance():
                    if sort_method == 'relevance-desc':
                        extra_kwargs['order_by'] = ['-relevance',]

                qs = qs.extra(**extra_kwargs)
            else:
                #fallback to dumb title match search
                qs = qs.extra(
                                where=['title like %s'], 
                                params=['%' + search_query + '%']
                            )

        #have to import this at run time, otherwise there
        #a circular import dependency...
        from askbot.conf import settings as askbot_settings
        if scope_selector:
            if scope_selector == 'unanswered':
                if askbot_settings.UNANSWERED_QUESTION_MEANING == 'NO_ANSWERS':
                    qs = qs.filter(answer_count=0)#todo: expand for different meanings of this
                elif askbot_settings.UNANSWERED_QUESTION_MEANING == 'NO_ACCEPTED_ANSWERS':
                    qs = qs.filter(answer_accepted=False)
                elif askbot_settings.UNANSWERED_QUESTION_MEANING == 'NO_UPVOTED_ANSWERS':
                    raise NotImplementedError()
                else:
                    raise Exception('UNANSWERED_QUESTION_MEANING setting is wrong')
            elif scope_selector == 'favorite':
                qs = qs.filter(favorited_by = request_user)
            
        #user contributed questions & answers
        if author_selector:
            try:
                #todo maybe support selection by multiple authors
                u = User.objects.get(id=int(author_selector))
                qs = qs.filter(
                            models.Q(author=u, deleted=False) \
                            | models.Q(answers__author=u, answers__deleted=False)
                        )
                meta_data['author_name'] = u.username
            except User.DoesNotExist:
                meta_data['author_name'] = None

        #get users tag filters
        ignored_tag_names = None
        if request_user and request_user.is_authenticated():
            uid_str = str(request_user.id)
            #mark questions tagged with interesting tags
            #a kind of fancy annotation, would be nice to avoid it
            interesting_tags = Tag.objects.filter(
                                    user_selections__user=request_user,
                                    user_selections__reason='good'
                                )
            ignored_tags = Tag.objects.filter(
                                    user_selections__user=request_user,
                                    user_selections__reason='bad'
                                )

            meta_data['interesting_tag_names'] = [tag.name for tag in interesting_tags]

            ignored_tag_names = [tag.name for tag in ignored_tags]
            meta_data['ignored_tag_names'] = ignored_tag_names

            if interesting_tags:
                #expensive query
                qs = qs.extra(
                    select = SortedDict([
                        (
                            'interesting_score', 
                            'SELECT COUNT(1) FROM askbot_markedtag, question_tags '
                             + 'WHERE askbot_markedtag.user_id = %s '
                             + 'AND askbot_markedtag.tag_id = question_tags.tag_id '
                             + 'AND askbot_markedtag.reason = \'good\' '
                             + 'AND question_tags.question_id = question.id'
                        ),
                            ]),
                    select_params = (uid_str,),
                 )
            # get the list of interesting and ignored tags (interesting_tag_names, ignored_tag_names) = (None, None)

            if ignored_tags:
                if request_user.hide_ignored_questions:
                    #exclude ignored tags if the user wants to
                    qs = qs.exclude(tags__in=ignored_tags)
                else:
                    #annotate questions tagged with ignored tags
                    #expensive query
                    qs = qs.extra(
                        select = SortedDict([
                            (
                                'ignored_score', 
                                'SELECT COUNT(1) '
                                  + 'FROM askbot_markedtag, question_tags '
                                  + 'WHERE askbot_markedtag.user_id = %s '
                                  + 'AND askbot_markedtag.tag_id = question_tags.tag_id '
                                  + 'AND askbot_markedtag.reason = \'bad\' '
                                  + 'AND question_tags.question_id = question.id'
                            )
                                ]),
                        select_params = (uid_str, )
                     )

        if sort_method != 'relevance-desc':
            #relevance sort is set in the extra statement
            #only for postgresql
            orderby = QUESTION_ORDER_BY_MAP[sort_method]
            qs = qs.order_by(orderby)

        qs = qs.distinct()
        qs = qs.select_related(
                        'last_activity_by__id',
                        'last_activity_by__username',
                        'last_activity_by__reputation',
                        'last_activity_by__gold',
                        'last_activity_by__silver',
                        'last_activity_by__bronze'
                    )

        related_tags = Tag.objects.get_related_to_search(
                                        questions = qs,
                                        search_state = search_state,
                                        ignored_tag_names = ignored_tag_names
                                    )
        return qs, meta_data, related_tags

    #todo: this function is similar to get_response_receivers
    #profile this function against the other one
    #todo: maybe this must be a query set method, not manager method
    def get_question_and_answer_contributors(self, question_list):
        answer_list = []
        #question_list = list(question_list)#important for MySQL, b/c it does not support
        from askbot.models.answer import Answer
        q_id = [question.id for question in question_list]
        a_id = list(Answer.objects.filter(question__in=q_id).values_list('id', flat=True))
        u_id = set(self.filter(id__in=q_id).values_list('author', flat=True))
        u_id = u_id.union(
                    set(Answer.objects.filter(id__in=a_id).values_list('author', flat=True))
                )
        contributors = User.objects.filter(id__in=u_id).order_by('?')
        #print contributors
        #could not optimize this query with indices so it was split into what's now above
        #contributors = User.objects.filter(
        #                            models.Q(questions__in=question_list) \
        #                            | models.Q(answers__in=answer_list)
        #                           ).distinct()
        #contributors = list(contributors)
        return contributors

    def get_author_list(self, **kwargs):
        #todo: - this is duplication - answer manager also has this method
        #will be gone when models are consolidated
        #note that method get_question_and_answer_contributors is similar in function
        authors = set()
        for question in self:
            authors.update(question.get_author_list(**kwargs))
        return list(authors)

    def update_view_count(self, question):
        """
        update counter+1 when user browse question page
        """
        self.filter(id=question.id).update(view_count = question.view_count + 1)


class Question(content.Content, DeletableContent):
    post_type = 'question'
    title    = models.CharField(max_length=300)
    tags     = models.ManyToManyField('Tag', related_name='questions')
    answer_accepted = models.BooleanField(default=False)
    closed          = models.BooleanField(default=False)
    closed_by       = models.ForeignKey(User, null=True, blank=True, related_name='closed_questions')
    closed_at       = models.DateTimeField(null=True, blank=True)
    close_reason    = models.SmallIntegerField(
                                            choices=const.CLOSE_REASONS, 
                                            null=True, 
                                            blank=True
                                        )
    followed_by     = models.ManyToManyField(User, related_name='followed_questions')

    # Denormalised data
    answer_count         = models.PositiveIntegerField(default=0)
    view_count           = models.PositiveIntegerField(default=0)
    favourite_count      = models.PositiveIntegerField(default=0)
    last_activity_at     = models.DateTimeField(default=datetime.datetime.now)
    last_activity_by     = models.ForeignKey(User, related_name='last_active_in_questions')
    tagnames             = models.CharField(max_length=125)
    summary              = models.CharField(max_length=180)

    favorited_by         = models.ManyToManyField(User, through='FavoriteQuestion', related_name='favorite_questions') 

    objects = QuestionManager()

    class Meta(content.Content.Meta):
        db_table = u'question'

    parse = parse_post_text
    parse_and_save = parse_and_save_post

    def assert_is_visible_to(self, user):
        """raises QuestionHidden"""
        if self.deleted:
            message = _(
                    'Sorry, this question has been '
                    'deleted and is no longer accessible'
                )
            if user.is_anonymous():
                raise exceptions.QuestionHidden(message)
            try:
                user.assert_can_see_deleted_post(self)
            except django_exceptions.PermissionDenied:
                raise exceptions.QuestionHidden(message)

    def update_answer_count(self, save = True):
        """updates the denormalized field 'answer_count'
        on the question
        """
        self.answer_count = self.get_answers().count()
        if save: 
            self.save()
   
    def update_favorite_count(self):
        """
        update favourite_count for given question
        """
        self.favourite_count = FavoriteQuestion.objects.filter(
                                                            question=self
                                                        ).count()
        self.save()


    def get_similar_questions(self):
        """
        Get 10 similar questions for given one.
        Questions with the individual tags will be added to list if above questions are not full.

        This function has a limitation that it will
        retrieve only 100 records then select 10 most similar
        from that list as querying entire database may
        be very expensive - this function will benefit from
        some sort of optimization
        """
        #print datetime.datetime.now()

        def get_data():

            tags_list = self.tags.all()
            similar_questions = self.__class__.objects.filter(
                                            tags__in = self.tags.all()
                                        ).exclude(
                                            id = self.id,
                                        ).exclude(
                                            deleted = True
                                        ).distinct()[:100]
            similar_questions = list(similar_questions)
            output = list()
            for question in similar_questions:
                question.similarity = self.get_similarity(
                                                    other_question = question
                                                )
            #sort in reverse order - x and y are interchanged in cmp() call
            similar_questions.sort(lambda x,y: cmp(y.similarity, x.similarity))
            if len(similar_questions) > 10:
                return similar_questions[:10]
            else:
                return similar_questions

        return LazyList(get_data)

    def get_page_number(self, answers = None):
        """question always appears on its own
        first page by definition. The answers 
        parameter is not used here. The extra parameter is necessary
        to maintain generality of the function call signature"""
        return 1

    def get_similarity(self, other_question = None):
        """return number of tags in the other question
        that overlap with the current question (self)
        """
        my_tags = set(self.get_tag_names())
        others_tags = set(other_question.get_tag_names())
        return len(my_tags & others_tags)

    def update_tags(self, tagnames = None, user = None, timestamp = None):
        """
        Updates Tag associations for a question to match the given
        tagname string.
        """

        previous_tags = list(self.tags.all())

        previous_tagnames = set([tag.name for tag in previous_tags])
        updated_tagnames = set(t for t in tagnames.split(' '))

        removed_tagnames = previous_tagnames - updated_tagnames
        added_tagnames = updated_tagnames - previous_tagnames

        modified_tags = list()
        #remove tags from the question's tags many2many relation
        if removed_tagnames:
            removed_tags = [tag for tag in previous_tags if tag.name in removed_tagnames]
            self.tags.remove(*removed_tags)

            #if any of the removed tags reached use count == 1 that means they must be deleted
            for tag in removed_tags:
                if tag.used_count == 1:
                    #we won't modify used count b/c it's done below anyway
                    removed_tags.remove(tag)
                    #todo - do we need to use fields deleted_by and deleted_at?
                    tag.delete()#auto-delete tags whose use count dwindled

            #remember modified tags, we'll need to update use counts on them
            modified_tags = removed_tags

        #add new tags to the relation
        if added_tagnames:
            #find reused tags
            reused_tags = Tag.objects.filter(name__in = added_tagnames)
            #undelete them, because we are using them
            reused_count = reused_tags.update(
                                    deleted = False,
                                    deleted_by = None,
                                    deleted_at = None
                                )
            #if there are brand new tags, create them and finalize the added tag list
            if reused_count < len(added_tagnames):
                added_tags = list(reused_tags)

                reused_tagnames = set([tag.name for tag in reused_tags])
                new_tagnames = added_tagnames - reused_tagnames
                for name in new_tagnames:
                    new_tag = Tag.objects.create(
                                            name = name,
                                            created_by = user,
                                            used_count = 1
                                        )
                    added_tags.append(new_tag)
            else:
                added_tags = reused_tags

            #finally add tags to the relation and extend the modified list
            self.tags.add(*added_tags)
            modified_tags.extend(added_tags)

        #if there are any modified tags, update their use counts
        if modified_tags:
            Tag.objects.update_use_counts(modified_tags)
            signals.tags_updated.send(None,
                                question = self,
                                tags = modified_tags,
                                user = user,
                                timestamp = timestamp
                            )
            return True

        return False

    def delete(self):
        super(Question, self).delete()
        try:
            from askbot.conf import settings as askbot_settings
            if askbot_settings.GOOGLE_SITEMAP_CODE != '':
                ping_google()
        except Exception:
            logging.debug('problem pinging google did you register you sitemap with google?')

    def get_answers(self, user = None):
        """returns query set for answers to this question
        that may be shown to the given user
        """

        if user is None or user.is_anonymous():
            return self.answers.filter(deleted=False)
        else:
            if user.is_administrator() or user.is_moderator():
                return self.answers.all()
            else:
                return self.answers.filter(
                                models.Q(deleted = False) | models.Q(author = user) \
                                | models.Q(deleted_by = user)
                            )

    def get_updated_activity_data(self, created = False):
        if created:
            return const.TYPE_ACTIVITY_ASK_QUESTION, self
        else:
            latest_revision = self.get_latest_revision()
            return const.TYPE_ACTIVITY_UPDATE_QUESTION, latest_revision

    def get_response_receivers(self, exclude_list = None):
        """returns list of users who might be interested
        in the question update based on their participation 
        in the question activity

        exclude_list is mandatory - it normally should have the
        author of the update so the he/she is not notified about the update
        """
        assert(exclude_list != None)
        recipients = set()
        recipients.update(
                            self.get_author_list(
                                    include_comments = True
                                )
                        )
        #do not include answer commenters here
        for a in self.answers.all():
            recipients.update(a.get_author_list())

        recipients -= set(exclude_list)
        return recipients

    def retag(self, retagged_by=None, retagged_at=None, tagnames=None, silent=False):
        if None in (retagged_by, retagged_at, tagnames):
            raise Exception('arguments retagged_at, retagged_by and tagnames are required')
        # Update the Question itself
        self.tagnames = tagnames
        if silent == False:
            self.last_edited_at = retagged_at
            #self.last_activity_at = retagged_at
            self.last_edited_by = retagged_by
            #self.last_activity_by = retagged_by
        self.save()

        # Update the Question's tag associations
        self.update_tags(tagnames = tagnames, user = retagged_by, timestamp = retagged_at)

        # Create a new revision
        latest_revision = self.get_latest_revision()
        QuestionRevision.objects.create(
            question   = self,
            title      = latest_revision.title,
            author     = retagged_by,
            revised_at = retagged_at,
            tagnames   = tagnames,
            summary    = const.POST_STATUS['retagged'],
            text       = latest_revision.text
        )

    def get_origin_post(self):
        return self

    def apply_edit(self, edited_at=None, edited_by=None, title=None,\
                    text=None, comment=None, tags=None, wiki=False):

        latest_revision = self.get_latest_revision()
        #a hack to allow partial edits - important for SE loader
        if title is None:
            title = self.title
        if text is None:
            text = latest_revision.text
        if tags is None:
            tags = latest_revision.tagnames

        if edited_by is None:
            raise Exception('parameter edited_by is required')

        if edited_at is None:
            edited_at = datetime.datetime.now()

        # Update the Question itself
        self.title = title
        self.last_edited_at = edited_at
        self.last_activity_at = edited_at
        self.last_edited_by = edited_by
        self.last_activity_by = edited_by
        self.tagnames = tags
        self.text = text

        #wiki is an eternal trap whence there is no exit
        if self.wiki == False and wiki == True:
            self.wiki = True

        # Update the Question tag associations
        if latest_revision.tagnames != tags:
            self.update_tags(tagnames = tags, user = edited_by, timestamp = edited_at)

        # Create a new revision
        self.add_revision(
            author = edited_by,
            text = text,
            revised_at = edited_at,
            comment = comment,
        )

        self.parse_and_save(author = edited_by)

    def add_revision(self,author=None, text=None, comment=None, revised_at=None):
        if None in (author, text, comment):
            raise Exception('author, text and comment are required arguments')
        rev_no = self.revisions.all().count() + 1
        if comment in (None, ''):
            if rev_no == 1:
                comment = const.POST_STATUS['default_version']
            else:
                comment = 'No.%s Revision' % rev_no
            
        return QuestionRevision.objects.create(
            question   = self,
            revision   = rev_no,
            title      = self.title,
            author     = author,
            revised_at = revised_at,
            tagnames   = self.tagnames,
            summary    = comment,
            text       = text
        )

    def get_tag_names(self):
        """Creates a list of Tag names from the ``tagnames`` attribute."""
        return self.tagnames.split(u' ')

    def set_tag_names(self, tag_names):
        """expects some iterable of unicode string tag names
        joins the names with a space and assigns to self.tagnames
        does not save the object
        """
        self.tagnames = u' '.join(tag_names)

    def tagname_meta_generator(self):
        return u','.join([unicode(tag) for tag in self.get_tag_names()])

    def get_absolute_url(self, no_slug = False):
        url = reverse('question', args=[self.id])
        if no_slug == True:
            return url
        else:
            return url + django_urlquote(slugify(self.title))

    def has_favorite_by_user(self, user):
        if not user.is_authenticated():
            return False

        return FavoriteQuestion.objects.filter(question=self, user=user).count() > 0

    def get_answer_count_by_user(self, user_id):
        from askbot.models.answer import Answer
        query_set = Answer.objects.filter(author__id=user_id)
        return query_set.filter(question=self).count()

    def get_question_title(self):
        if self.closed:
            attr = const.POST_STATUS['closed']
        elif self.deleted:
            attr = const.POST_STATUS['deleted']
        else:
            attr = None
        if attr is not None:
            return u'%s %s' % (self.title, attr)
        else:
            return self.title

    def get_revision_url(self):
        return reverse('question_revisions', args=[self.id])

    def get_last_update_info(self):
        when, who = self.post_get_last_update_info()

        answers = self.answers.all()
        if len(answers) > 0:
            for a in answers:
                a_when, a_who = a.post_get_last_update_info()
                if a_when > when:
                    when = a_when
                    who = a_who

        return when, who

    def get_update_summary(self,last_reported_at=None,recipient_email=''):
        edited = False
        if self.last_edited_at and self.last_edited_at > last_reported_at:
            if self.last_edited_by.email != recipient_email:
                edited = True
        comments = []
        for comment in self.comments.all():
            if comment.added_at > last_reported_at and comment.user.email != recipient_email:
                comments.append(comment)
        new_answers = []
        answer_comments = []
        modified_answers = []
        commented_answers = []
        import sets
        commented_answers = sets.Set([])
        for answer in self.answers.all():
            if (answer.added_at > last_reported_at and answer.author.email != recipient_email):
                new_answers.append(answer)
            if (answer.last_edited_at
                and answer.last_edited_at > last_reported_at
                and answer.last_edited_by.email != recipient_email):
                modified_answers.append(answer)
            for comment in answer.comments.all():
                if comment.added_at > last_reported_at and comment.user.email != recipient_email:
                    commented_answers.add(answer)
                    answer_comments.append(comment)

        #create the report
        from askbot.conf import settings as askbot_settings
        if edited or new_answers or modified_answers or answer_comments:
            out = []
            if edited:
                out.append(_('%(author)s modified the question') % {'author':self.last_edited_by.username})
            if new_answers:
                names = sets.Set(map(lambda x: x.author.username,new_answers))
                people = ', '.join(names)
                out.append(_('%(people)s posted %(new_answer_count)s new answers') \
                                % {'new_answer_count':len(new_answers),'people':people})
            if comments:
                names = sets.Set(map(lambda x: x.user.username,comments))
                people = ', '.join(names)
                out.append(_('%(people)s commented the question') % {'people':people})
            if answer_comments:
                names = sets.Set(map(lambda x: x.user.username,answer_comments))
                people = ', '.join(names)
                if len(commented_answers) > 1:
                    out.append(_('%(people)s commented answers') % {'people':people})
                else:
                    out.append(_('%(people)s commented an answer') % {'people':people})
            url = askbot_settings.APP_URL + self.get_absolute_url()
            retval = '<a href="%s">%s</a>:<br>\n' % (url,self.title)
            out = map(lambda x: '<li>' + x + '</li>',out)
            retval += '<ul>' + '\n'.join(out) + '</ul><br>\n'
            return retval
        else:
            return None

    def __unicode__(self):
        return self.title

        
class QuestionView(models.Model):
    question = models.ForeignKey(Question, related_name='viewed')
    who = models.ForeignKey(User, related_name='question_views')
    when = models.DateTimeField()

    class Meta:
        app_label = 'askbot'

class FavoriteQuestion(models.Model):
    """A favorite Question of a User."""
    question      = models.ForeignKey(Question)
    user          = models.ForeignKey(User, related_name='user_favorite_questions')
    added_at      = models.DateTimeField(default=datetime.datetime.now)

    class Meta:
        app_label = 'askbot'
        db_table = u'favorite_question'
    def __unicode__(self):
        return '[%s] favorited at %s' %(self.user, self.added_at)

QUESTION_REVISION_TEMPLATE = ('<h3>%(title)s</h3>\n'
                              '<div class="text">%(html)s</div>\n'
                              '<div class="tags">%(tags)s</div>')
class QuestionRevision(ContentRevision):
    """A revision of a Question."""
    question   = models.ForeignKey(Question, related_name='revisions')
    title      = models.CharField(max_length=300)
    tagnames   = models.CharField(max_length=125)

    class Meta(ContentRevision.Meta):
        db_table = u'question_revision'
        ordering = ('-revision',)

    def get_question_title(self):
        return self.question.title

    def get_absolute_url(self):
        #print 'in QuestionRevision.get_absolute_url()'
        return reverse('question_revisions', args=[self.question.id])

    def as_html(self):
        markdowner = markup.get_parser()
        return QUESTION_REVISION_TEMPLATE % {
            'title': self.title,
            'html': sanitize_html(markdowner.convert(self.text)),
            'tags': ' '.join(['<a class="post-tag">%s</a>' % tag
                              for tag in self.tagnames.split(' ')]),
        }

    def save(self, **kwargs):
        """Looks up the next available revision number."""
        if not self.revision:
            self.revision = QuestionRevision.objects.filter(
                question=self.question).values_list('revision',
                                                    flat=True)[0] + 1
        super(QuestionRevision, self).save(**kwargs)

    def __unicode__(self):
        return u'revision %s of %s' % (self.revision, self.title)

class AnonymousQuestion(AnonymousContent):
    title = models.CharField(max_length=300)
    tagnames = models.CharField(max_length=125)

    def publish(self,user):
        added_at = datetime.datetime.now()
        Question.objects.create_new(
                                title=self.title,
                                author=user,
                                added_at=added_at,
                                wiki=self.wiki,
                                tagnames=self.tagnames,
                                text=self.text,
                                )
        self.delete()
